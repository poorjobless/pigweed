#!/usr/bin/env python3

# Copyright 2019 The Pigweed Authors
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.
"""Checks and fixes formatting for source files.

This uses clang-format, gn format, gofmt, and python -m yapf to format source
code. These tools must be available on the path when this script is invoked!
"""

import argparse
import collections
import difflib
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable, Collection, Dict, Iterable, List, NamedTuple
from typing import Optional, Sequence

try:
    import pw_presubmit
except ImportError:
    # Append the pw_presubmit package path to the module search path to allow
    # running this module without installing the pw_presubmit package.
    sys.path.append(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    import pw_presubmit

from pw_presubmit import list_git_files, log_run, plural

_LOG: logging.Logger = logging.getLogger(__name__)


def _colorize_diff_line(line: str) -> str:
    if line.startswith('--- ') or line.startswith('+++ '):
        return pw_presubmit.color_bold_white(line)
    if line.startswith('-'):
        return pw_presubmit.color_red(line)
    if line.startswith('+'):
        return pw_presubmit.color_green(line)
    if line.startswith('@@ '):
        return pw_presubmit.color_aqua(line)
    return line


def colorize_diff(lines: Iterable[str]) -> str:
    """Takes a diff str or list of str lines and returns a colorized version."""
    if isinstance(lines, str):
        lines = lines.splitlines(True)

    return ''.join(_colorize_diff_line(line) for line in lines)


def _diff(path, original: bytes, formatted: bytes) -> str:
    return colorize_diff(
        difflib.unified_diff(
            original.decode(errors='replace').splitlines(True),
            formatted.decode(errors='replace').splitlines(True),
            f'{path}  (original)', f'{path}  (reformatted)'))


Formatter = Callable[[str, bytes], bytes]


def _diff_formatted(path, formatter: Formatter) -> Optional[str]:
    """Returns a diff comparing a file to its formatted version."""
    with open(path, 'rb') as fd:
        original = fd.read()

    formatted = formatter(path, original)

    return None if formatted == original else _diff(path, original, formatted)


def _check_files(files, formatter: Formatter) -> Dict[str, str]:
    errors = {}

    for path in files:
        difference = _diff_formatted(path, formatter)
        if difference:
            errors[path] = difference

    return errors


def _clang_format(*args: str, **kwargs) -> bytes:
    return log_run('clang-format',
                   '--style=file',
                   *args,
                   stdout=subprocess.PIPE,
                   check=True,
                   **kwargs).stdout


def check_c_format(files: Iterable) -> Dict[str, str]:
    """Checks formatting; returns {path: diff} for files with bad formatting."""
    return _check_files(files, lambda path, _: _clang_format(path))


def fix_c_format(files: Iterable) -> None:
    """Fixes formatting for the provided files in place."""
    _clang_format('-i', *files)


def check_gn_format(files: Iterable) -> Dict[str, str]:
    """Checks formatting; returns {path: diff} for files with bad formatting."""
    return _check_files(
        files, lambda _, data: log_run('gn',
                                       'format',
                                       '--stdin',
                                       input=data,
                                       stdout=subprocess.PIPE,
                                       check=True).stdout)


def fix_gn_format(files: Iterable) -> None:
    """Fixes formatting for the provided files in place."""
    log_run('gn', 'format', *files, check=True)


def check_go_format(files: Iterable) -> Dict[str, str]:
    """Checks formatting; returns {path: diff} for files with bad formatting."""
    return _check_files(
        files, lambda path, _: log_run(
            'gofmt', path, stdout=subprocess.PIPE, check=True).stdout)


def fix_go_format(files: Iterable) -> None:
    """Fixes formatting for the provided files in place."""
    log_run('gofmt', '-w', *files, check=True)


def _yapf(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return log_run('python',
                   '-m',
                   'yapf',
                   '--parallel',
                   *args,
                   capture_output=True,
                   **kwargs)


_DIFF_START = re.compile(r'^--- (.*)\s+\(original\)$', flags=re.MULTILINE)


def check_py_format(files: Iterable) -> Dict[str, str]:
    """Checks formatting; returns {path: diff} for files with bad formatting."""
    process = _yapf('--diff', *files)

    errors = {}

    if process.stdout:
        raw_diff = process.stdout.decode(errors='replace')

        matches = tuple(_DIFF_START.finditer(raw_diff))
        for start, end in zip(matches, (*matches[1:], None)):
            errors[start.group(1)] = colorize_diff(
                raw_diff[start.start():end.start() if end else None])

    if process.stderr:
        _LOG.error('yapf encountered an error:\n%s',
                   process.stderr.decode(errors='replace').rstrip())
        errors.update({file: '' for file in files if file not in errors})

    return errors


def fix_py_format(files: Iterable):
    """Fixes formatting for the provided files in place."""
    _yapf('--in-place', *files, check=True)


def print_format_check(
        errors: Dict[str, str],
        show_fix_commands: bool,
) -> None:
    """Prints and returns the result of a check_*_format function."""
    if not errors:
        # Don't print anything in the all-good case.
        return

    # Show the format fixing diff suggested by the tooling (with colors).
    _LOG.warning('Found %d files with formatting errors. Format changes:',
                 len(errors))
    for diff in errors.values():
        print(diff, end='')

    # Show a copy-and-pastable command to fix the issues.
    if show_fix_commands:

        def path_relative_to_cwd(path):
            try:
                return Path(path).resolve().relative_to(Path.cwd().resolve())
            except ValueError:
                return Path(path).resolve()

        message = (f'  pw format --fix {path_relative_to_cwd(path)}'
                   for path in errors)
        _LOG.warning('To fix formatting, run:\n\n%s\n', '\n'.join(message))


class CodeFormat(NamedTuple):
    language: str
    extensions: Collection[str]
    check: Callable[[Iterable], Dict[str, str]]
    fix: Callable[[Iterable], None]


C_FORMAT: CodeFormat = CodeFormat(
    'C and C++', frozenset(['.h', '.hh', '.hpp', '.c', '.cc', '.cpp']),
    check_c_format, fix_c_format)

GN_FORMAT: CodeFormat = CodeFormat('GN', ('.gn', '.gni'), check_gn_format,
                                   fix_gn_format)

GO_FORMAT: CodeFormat = CodeFormat('Go', ('.go', ), check_go_format,
                                   fix_go_format)

PYTHON_FORMAT: CodeFormat = CodeFormat('Python', ('.py', ), check_py_format,
                                       fix_py_format)

CODE_FORMATS: Sequence[CodeFormat] = (
    C_FORMAT,
    GN_FORMAT,
    GO_FORMAT,
    PYTHON_FORMAT,
)


def presubmit_check(code_format: CodeFormat) -> Callable:
    """Creates a presubmit check function from a CodeFormat object."""
    @pw_presubmit.filter_paths(endswith=code_format.extensions)
    def check_code_format(ctx: pw_presubmit.PresubmitContext):
        errors = code_format.check(ctx.paths)
        print_format_check(
            errors,
            # When running as part of presubmit, show the fix command help.
            show_fix_commands=True,
        )
        if errors:
            raise pw_presubmit.PresubmitFailure

    check_code_format.__name__ = f'{code_format.language} format'

    return check_code_format


PRESUBMIT_CHECKS: Sequence[Callable] = tuple(
    presubmit_check(code_format) for code_format in CODE_FORMATS)


class CodeFormatter:
    """Checks or fixes the formatting of a set of files."""
    def __init__(self, files: Sequence[str]):
        self.paths = list(files)
        self._formats: Dict[CodeFormat, List] = collections.defaultdict(list)

        for path in files:
            for code_format in CODE_FORMATS:
                if any(path.endswith(e) for e in code_format.extensions):
                    self._formats[code_format].append(path)

    def check(self) -> Dict[str, str]:
        """Returns {path: diff} for files with incorrect formatting."""
        errors = {}

        for code_format, files in self._formats.items():
            _LOG.debug('Checking %s', ', '.join(files))
            errors.update(code_format.check(files))

        return collections.OrderedDict(sorted(errors.items()))

    def fix(self) -> None:
        """Fixes format errors for supported files in place."""
        for code_format, files in self._formats.items():
            code_format.fix(files)
            _LOG.info('Formatted %s',
                      plural(files, code_format.language + ' file'))


def main(paths: Sequence[Path], exclude, base: str, fix: bool) -> int:
    """Checks or fixes formatting for files in a Git repo."""
    files = [str(path.resolve()) for path in paths if path.is_file()]

    # If this is a Git repo, list the original paths with git ls-files or diff.
    if pw_presubmit.is_git_repo():
        repo = pw_presubmit.git_repo_path()
        if repo.samefile(Path.cwd()):
            _LOG.info('Checking files in the %s repository', repo)
        else:
            _LOG.info(
                'Checking files in the %s subdirectory of the %s repository',
                Path.cwd().relative_to(repo), repo)

        # Add files from Git and remove duplicates.
        file_set = set(str(p) for p in list_git_files(base, paths, exclude))
        files = sorted(file_set | set(files))
    elif base:
        _LOG.critical(
            'A base commit may only be provided if running from a Git repo')
        return 1

    formatter = CodeFormatter(files)

    _LOG.debug('Found %s files:\n%s', len(files), '\n'.join(f for f in files))
    _LOG.info('Checking formatting for %s', plural(formatter.paths, 'file'))

    errors = formatter.check()
    print_format_check(errors, show_fix_commands=(not fix))

    if errors:
        if fix:
            formatter.fix()
            # TODO: This should perhaps check that the fixes were successful.
            _LOG.info('Formatting fixes applied successfully')
            return 0

        _LOG.error('Formatting errors found')
        return 1

    _LOG.info('Congratulations! No formatting changes needed')
    return 0


def argument_parser(parser=None) -> argparse.ArgumentParser:
    if parser is None:
        parser = argparse.ArgumentParser(description=__doc__)

    pw_presubmit.add_path_arguments(parser)

    parser.add_argument('--fix',
                        action='store_true',
                        help='Apply formatting fixes in place.')

    return parser


if __name__ == '__main__':
    logging.basicConfig(format='%(message)s', level=logging.INFO)
    sys.exit(main(**vars(argument_parser().parse_args())))
