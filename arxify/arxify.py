from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from itertools import chain
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable


def find_files(root: Path) -> list[Path]:
    own_files = (f for f in root.iterdir() if f.is_file())
    sub_files = (find_files(d) for d in root.iterdir() if d.is_dir())
    return list(chain(own_files, *sub_files))


def remove_comment(line: str):
    match = re.search(r"(?<!\\)%", line)
    if match:
        return line[: match.start()]
    else:
        return line


def process_tex_file(path: Path) -> str:
    with path.open() as f:
        tex_code = f.read()
    lines = tex_code.split("\n")
    # Remove all comments and disable tikz externalize if enabled
    lines_filtered = [remove_comment(l) for l in lines if not l.strip().startswith("%")]
    return "\n".join(lines_filtered)


def parse_fls_inputs(fls_path: Path, root: Path) -> set[Path]:
    # The .fls file is produced by pdflatex/lualatex with -recorder. Every line
    # has one of three fixed prefixes: "PWD <dir>", "INPUT <path>", or
    # "OUTPUT <path>". Relative paths are relative to PWD.
    pwd = None
    inputs = set()
    for line in fls_path.read_text().splitlines():
        prefix, sep, rest = line.partition(" ")
        if not sep:
            continue
        if prefix == "PWD":
            pwd = Path(rest)
        elif prefix == "INPUT":
            p = Path(rest)
            if not p.is_absolute() and pwd is not None:
                p = pwd / p
            try:
                p = p.resolve()
            except OSError:
                continue
            if root in p.parents or p == root:
                inputs.add(p)
    return inputs


def compile_and_find_required_files(
    root: Path,
    main_tex_file_rel: Path,
    latex_out: Path,
    compiler: str = "pdflatex",
    shell_escape: bool = False,
) -> set[Path]:
    subprocess.check_call(
        [
            compiler,
            *(["--shell-escape"] if shell_escape else []),
            "--interaction=nonstopmode",
            "--halt-on-error",
            "-recorder",
            "--output-directory",
            str(latex_out),
            str(main_tex_file_rel),
        ],
        cwd=root,
    )

    fls_path = latex_out / (main_tex_file_rel.stem + ".fls")
    if not fls_path.exists():
        raise RuntimeError(
            f"Expected recorder file {fls_path} was not produced by {compiler}."
        )
    return parse_fls_inputs(fls_path, root.resolve())


def find_tikz_externalize_dirs(search_files: Iterable[Path]) -> list[Path]:
    output = []
    for f in search_files:
        if f.suffix == ".tex":
            matches = re.findall(
                r"\\tikzexternalize\[(?:.*,)?prefix=([^,\]]+)(?:,.*)?]", f.read_text()
            )
            output += [Path(match) for match in matches]
    return output


def copy_dirs(src: Path, dst: Path):
    for item in src.iterdir():
        if item.is_dir():
            new_dst = dst / item.name
            new_dst.mkdir(exist_ok=True)
            copy_dirs(item, new_dst)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("main_tex_file", type=str, help="Main tex file of the project.")
    parser.add_argument(
        "output_filename", type=str, help="Filename of the output *.zip file."
    )
    parser.add_argument(
        "-c",
        "--compiler",
        choices=["pdflatex", "lualatex"],
        default="pdflatex",
        help="Compiler used to compile the project (default: pdflatex).",
    )
    parser.add_argument(
        "-b",
        "--bibliography-processor",
        choices=["bibtex", "biber"],
        default="bibtex",
        help="Which program to use for processing the bibliography (default: bibtex).",
    )
    parser.add_argument(
        "-i",
        "--include",
        nargs="+",
        default=(),
        help="Include these files, whether they are needed or not.",
    )
    parser.add_argument(
        "-r",
        "--root",
        type=str,
        help="Root directory of the project (default: parent of the main tex file).",
    )
    parser.add_argument(
        "--shell-escape",
        action="store_true",
        help="Enable shell escape for the LaTeX compiler.",
    )
    args = parser.parse_args()

    main_tex_file = Path(args.main_tex_file).resolve()
    if args.root is None:
        root = main_tex_file.parent
    else:
        root = Path(args.root).resolve()
        assert str(main_tex_file).startswith(str(root))
    main_tex_file_rel = main_tex_file.relative_to(root)

    with TemporaryDirectory() as td:
        # Resolve to follow symlinks like macOS's /var -> /private/var, so that
        # paths recorded by pdflatex (which are always real paths) match.
        td_path = Path(td).resolve()
        tmp_root = td_path / "root"
        latex_out = td_path / "out"
        zip_path = td_path / "zip"
        latex_out.mkdir()  # Copy all directories as tikz externalize might need one of them

        print("Copying files to temporary directory...")
        shutil.copytree(root, tmp_root)
        print("Done copying files.")

        copy_dirs(tmp_root, latex_out)
        additional_files = [
            (f if f.is_absolute() else root / f).resolve()
            for f in map(Path, args.include)
        ]
        additional_files_tmp = []
        for f in additional_files:
            if root not in f.parents:
                sys.stderr.write(
                    "Manually included file {} is not in a subdirectory of {}.".format(
                        f, root
                    )
                )
                exit(1)
            if not f.exists():
                sys.stderr.write(
                    "Manually included file {} does not exist.".format(f, root)
                )
                exit(1)
            new_path = tmp_root / f.relative_to(root)
            shutil.copy(f, new_path)
            additional_files_tmp.append(new_path)

        print("Stripping comments of tex files and disabling tikzexternalize...")
        for tf in tmp_root.rglob("*.tex"):
            new_content = process_tex_file(tf)
            with tf.open("w") as f:
                f.write(new_content)
        print("Done stripping comments.")

        # Pass 1
        print("Compiling LaTeX files...")
        required_files_1 = compile_and_find_required_files(
            tmp_root,
            main_tex_file_rel,
            latex_out,
            compiler=args.compiler,
            shell_escape=args.shell_escape,
        )
        tikz_externalize_dirs = find_tikz_externalize_dirs(required_files_1)
        print("Done compiling LaTeX files.")

        if len(tikz_externalize_dirs) == 0:
            required_files = required_files_1
        else:
            print(
                f"Found tikz externalize directories: {', '.join(map(str, tikz_externalize_dirs))}."
            )
            print(
                "Compiling LaTeX for the second time to include tikz externalize files..."
            )

            for d in tikz_externalize_dirs:
                shutil.copytree(latex_out / d, tmp_root / d, dirs_exist_ok=True)

            # Pass 2 to include tikz externalized files
            shutil.rmtree(latex_out)
            latex_out.mkdir()
            copy_dirs(tmp_root, latex_out)
            required_files = compile_and_find_required_files(
                tmp_root,
                main_tex_file_rel,
                latex_out,
                compiler=args.compiler,
                shell_escape=args.shell_escape,
            )
            print("Done compiling LaTeX files.")

        required_files.update(additional_files_tmp)

        bib_files = [
            str(path.relative_to(latex_out).with_suffix(""))
            for path in latex_out.rglob("*.aux")
            if any(
                re.match(r"\\bib(style|data)\{[^}]*}", l)
                for l in path.read_text().splitlines()
            )
        ]

        for bib_file in bib_files:
            if args.bibliography_processor == "biber":
                subprocess.check_call(
                    ["biber", "--input-directory", str(root), bib_file],
                    cwd=latex_out,
                )
            else:
                subprocess.check_call(
                    [args.bibliography_processor, bib_file],
                    cwd=latex_out,
                    env={
                        "BIBINPUTS": f"{root}:{os.environ.get('BIBINPUTS', '')}",
                        "BSTINPUTS": f"{root}:{os.environ.get('BSTINPUTS', '')}",
                        **os.environ,
                    },
                )

        print("The following source files will be included in the zip archive:")
        zip_path.mkdir()
        for tf in sorted(required_files):
            output_path = zip_path / tf.relative_to(tmp_root)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if tf.suffix != ".bib" and tf.exists():
                print("  {}".format(tf.relative_to(tmp_root)))
                shutil.copy(tf, output_path)

        for bib_file in bib_files:
            shutil.copy(latex_out / f"{bib_file}.bbl", zip_path / f"{bib_file}.bbl")

        output_path = Path(args.output_filename).resolve()
        if output_path.suffix == ".zip":
            output_path = output_path.with_suffix("")
        shutil.make_archive(str(output_path), "zip", zip_path)


if __name__ == "__main__":
    main()
