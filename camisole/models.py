# This file is part of Camisole.
#
# Copyright (c) 2016 Antoine Pietri <antoine.pietri@prologin.org>
# Copyright (c) 2016 Alexandre Macabies <alexandre.macabies@prologin.org>
# Copyright (c) 2016 Association Prologin <info@prologin.org>
#
# Camisole is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Prologin-SADM is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Prologin-SADM.  If not, see <http://www.gnu.org/licenses/>.

import functools
import logging
import os
import re
import subprocess
import tempfile
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Type

import camisole.isolate
import camisole.utils
from camisole.conf import conf


class Program:
    def __init__(self, cmd, *, opts=None, env=None,
                 version_opt='--version', version_lines=1,
                 version_regex=r'[0-9]+(\.[0-9]+)+'):
        self.cmd = camisole.utils.which(cmd)
        self.cmd_name = cmd
        self.opts = opts or []
        self.env = env or {}
        self.version_opt = version_opt
        self.version_lines = version_lines
        self.version_regex = re.compile(version_regex)

    @functools.lru_cache()
    def _version(self):
        if self.version_opt is None:  # noqa
            return None
        proc = subprocess.run([self.cmd, self.version_opt],
                              stderr=subprocess.STDOUT, stdout=subprocess.PIPE)
        return proc.stdout.decode().strip()

    def version(self):
        if self.version_opt is None:  # noqa
            return None
        res = self.version_regex.search(self._version())
        return res.group(0) if res else None

    def long_version(self):
        if self.version_opt is None:
            return None
        return '\n'.join(self._version().split('\n')[:self.version_lines])


class MetaLang(type):
    """Metaclass to customize Lang subclasses __repr__()"""

    def __repr__(self):
        return "<{realname}{name}>".format(
            realname=self.__name__,
            name=f' “{self.name}”' if self.__name__ != self.name else '')


class Lang(metaclass=MetaLang):
    """
    Abstract language descriptor.

    Subclass and define the relevant attributes and methods, if need be.
    """
    _registry: Dict[str, Type['Lang']] = {}
    _full_registry: Dict[str, Type['Lang']] = {}
    name: Optional[str] = None

    source_ext: Optional[str] = None
    compiler: Optional[Program] = None
    interpreter: Optional[Program] = None
    allowed_dirs: List[str] = []
    extra_binaries: Dict[str, Program] = {}
    reference_source: Optional[str] = None

    def __init_subclass__(cls, register=True, name=None, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.name = name or cls.__name__

        if not register:
            return

        registry_name = cls.name.lower()
        cls._full_registry[registry_name] = cls

        for binary in cls.required_binaries():
            if binary is not None and not os.access(binary.cmd, os.X_OK):
                logging.info(f'{cls.name}: cannot access `{binary.cmd}`, '
                             'language not loaded')
                return

        if registry_name in cls._registry:
            full_name = lambda c: f"{c.__module__}.{c.__qualname__}"
            warnings.warn(f"Lang registry: name '{registry_name}' for "
                          f"{full_name(cls)} overwrites "
                          f"{full_name(Lang._registry[registry_name])}")

        cls._registry[registry_name] = cls

    def __init__(self, opts):
        self.opts = opts

    @classmethod
    def required_binaries(cls):
        if cls.compiler:
            yield cls.compiler
        if cls.interpreter:
            yield cls.interpreter
        yield from cls.extra_binaries.values()

    @classmethod
    def programs(cls):
        return {p.cmd_name: {'version': p.version(), 'opts': p.opts}
                for p in cls.required_binaries()}

    async def compile(self):
        if not self.compiler:
            raise RuntimeError("no compiler")

        # We give compilers a nice /tmp playground
        root_tmp = tempfile.TemporaryDirectory(prefix='camisole-tmp-')
        os.chmod(root_tmp.name, 0o777)
        tmparg = [f'/tmp={root_tmp.name}:rw']

        isolator = camisole.isolate.Isolator(
            self.opts.get('compile', {}),
            allowed_dirs=self.get_allowed_dirs() + tmparg)
        async with isolator:
            wd = Path(isolator.path)
            env = {'HOME': self.filter_box_prefix(str(wd))}
            source = wd / self.source_filename()
            compiled = wd / self.execute_filename()
            with source.open('wb') as sourcefile:
                sourcefile.write(
                    camisole.utils.force_bytes(self.opts.get('source', '')))
            cmd = self.compile_command(str(source), str(compiled))
            await isolator.run(cmd, env={**env, **self.compiler.env})
            binary = self.read_compiled(str(compiled), isolator)

        root_tmp.cleanup()

        return (isolator.isolate_retcode, isolator.info, binary)

    async def execute(self, binary, opts=None):
        if opts is None:
            opts = {}
        opts = {**self.opts.get('execute', {}), **opts}
        input_data = None
        if 'stdin' in opts and opts['stdin']:
            input_data = camisole.utils.force_bytes(opts['stdin'])

        isolator = camisole.isolate.Isolator(
            opts, allowed_dirs=self.get_allowed_dirs())
        async with isolator:
            wd = isolator.path
            env = {'HOME': self.filter_box_prefix(str(wd))}
            compiled = self.write_binary(Path(wd), binary)
            env = {**env, **(self.interpreter.env if self.interpreter else {})}
            await isolator.run(self.execute_command(str(compiled)),
                               env=env, data=input_data)
        return (isolator.isolate_retcode, isolator.info)

    async def run_compilation(self, result):
        if self.compiler is not None:
            cretcode, info, binary = await self.compile()
            result['compile'] = info
            if cretcode != 0:
                return
            if binary is None:
                if result['compile']['stderr'].strip():
                    result['compile']['stderr'] += b'\n\n'
                result['compile']['stderr'] += b'Cannot find result binary.\n'
                return
        else:
            binary = camisole.utils.force_bytes(self.opts.get('source', ''))
        return binary

    async def run_tests(self, binary, result):
        tests = self.opts.get('tests', [{}])
        if tests:
            result['tests'] = [{}] * len(tests)
        is_shorted = False
        for i, test in enumerate(tests):
            test_name = test.get('name', 'test{:03d}'.format(i))
            if is_shorted:
                short_meta_defaults = {
                    'cg-mem': 0,
                    'cg-oom-killed': 0,
                    'csw-forced': 0,
                    'csw-voluntary': 0,
                    'exitcode': 0,
                    'exitsig': 0,
                    'exitsig-message': None,
                    'killed': False,
                    'max-rss': 0,
                    'message': None,
                    'status': 'SHORT_CIRCUIT',
                    'time': 0.0,
                    'time-wall': 0.0,
                }
                result['tests'][i] = {
                    'name': test_name,
                    'meta': short_meta_defaults
                }
                continue
            retcode, info = await self.execute(binary, test)
            result['tests'][i] = {
                'name': test_name,
                **info
            }

            if info['meta']['status'] in ['TIMED_OUT', 'RUNTIME_ERROR']:
                is_shorted = True

            if retcode != 0 and (
                    test.get('fatal', False) or
                    self.opts.get('all_fatal', False)):
                break

    async def run(self):
        result = {}
        binary = await self.run_compilation(result)
        if not binary:
            return result
        await self.run_tests(binary, result)
        return result

    def get_allowed_dirs(self):
        allowed_dirs = []
        allowed_dirs += self.allowed_dirs
        allowed_dirs += conf['allowed-dirs']
        return list(camisole.utils.uniquify(allowed_dirs))

    def compile_opt_out(self, output):
        return ['-o', output]

    def read_compiled(self, path, isolator):
        try:
            with Path(path).open('rb') as c:
                return c.read()
        except (FileNotFoundError, PermissionError):
            pass

    def write_binary(self, path, binary):
        compiled = path / self.execute_filename()
        with compiled.open('wb') as c:
            c.write(binary)
        compiled.chmod(0o700)
        return compiled

    def source_filename(self):
        return 'source' + self.source_ext

    def execute_filename(self):
        if self.compiler is None and self.source_ext:
            return 'compiled' + self.source_ext

        return 'compiled'

    @staticmethod
    def filter_box_prefix(s):
        return re.sub('/var/(local/)?lib/isolate/[0-9]+', '', s)

    def compile_command(self, source, output):
        if self.compiler is None:
            return None
        return [self.compiler.cmd,
                *self.compiler.opts,
                *self.compile_opt_out(self.filter_box_prefix(output)),
                self.filter_box_prefix(source)]

    def execute_command(self, output):
        cmd = []
        if self.interpreter is not None:
            cmd += [self.interpreter.cmd] + self.interpreter.opts
        return cmd + [self.filter_box_prefix(output)]


class PipelineLang(Lang, register=False):
    """
    A meta-language that compiles multiple sub-languages, passing the
    compilation result to the next sub-language, and eventually executing the
    last result.

    Subclass and define the ``sub_langs`` attribute.
    """
    sub_langs: List[Type[Lang]] = []

    @classmethod
    def required_binaries(cls):
        yield from super().required_binaries()
        for sub in cls.sub_langs:
            yield from sub.required_binaries()

    async def run_compilation(self, result):
        source = camisole.utils.force_bytes(self.opts.get('source', ''))
        for i, lang_cls in enumerate(self.sub_langs):
            lang = lang_cls({**self.opts, 'source': source})
            cretcode, info, binary = await lang.compile()
            result['compile'] = info
            if cretcode != 0:
                return
            if binary is None:
                if result['compile']['stderr'].strip():
                    result['compile']['stderr'] += b'\n\n'
                result['compile']['stderr'] += b'Cannot find result binary.\n'
                return
            # compile output is next stage input
            source = binary
        return binary

    async def compile(self):
        raise NotImplementedError()

class InteractiveLang:
    def __init__(self, lang_prog, lang_interact):
        self.lang_prog = lang_prog
        self.lang_interact = lang_interact
    
    async def run(self):
        result_prog = {}
        result_interact = {}
        binary_prog = await self.lang_prog.run_compilation(result_prog)
        binary_interact = await self.lang_interact.run_compilation(result_interact)
        if not binary_prog or not binary_interact:
            return {'prog': result_prog, 'interact': result_interact}
        await self.run_tests(binary_prog, binary_interact, result_prog, result_interact)
        return {'prog': result_prog, 'interact': result_interact}
        
    async def execute(self, binary_prog, binary_interact, opts=None):
        if opts is None:
            opts = {}
        opts_prog = {**self.lang_prog.opts.get('execute', {}), **opts}
        opts_interact = {**self.lang_interact.opts.get('execute', {}), **opts}

        input_data = None
        if 'stdin' in opts and opts['stdin']:
            input_data = camisole.utils.force_bytes(opts['stdin'])

        isolator_prog = camisole.isolate.Isolator(
            opts_prog, allowed_dirs=self.lang_prog.get_allowed_dirs())
        isolator_interact = camisole.isolate.Isolator(
            opts_interact, allowed_dirs=self.lang_interact.get_allowed_dirs())
        async with isolator_prog, isolator_interact:
            wd_prog = isolator_prog.path
            wd_interact = isolator_interact.path
            env_prog = {'HOME': self.lang_prog.filter_box_prefix(str(wd_prog))}
            env_interact = {'HOME': self.lang_interact.filter_box_prefix(str(wd_interact))}
            compiled_prog = self.lang_prog.write_binary(Path(wd_prog), binary_prog)
            compiled_interact = self.lang_interact.write_binary(Path(wd_interact), binary_interact)
            input_file = Path(wd_interact) / 'input.txt'
            with input_file.open('wb') as f:
                f.write(input_data)
            env_prog = {**env_prog, **(self.lang_prog.interpreter.env if self.lang_prog.interpreter else {})}
            env_interact = {**env_interact, **(self.lang_interact.interpreter.env if self.lang_interact.interpreter else {})}
            await camisole.isolate.Isolator.run_interactive(isolator_prog, isolator_interact, self.lang_prog.execute_command(str(compiled_prog)), self.lang_interact.execute_command(str(compiled_interact)) + ['input.txt'], env_prog, env_interact)

        return (isolator_prog.isolate_retcode, isolator_prog.info, isolator_interact.isolate_retcode, isolator_interact.info)

    async def run_tests(self, binary_prog, binary_interact, result_prog, result_interact):
        tests = self.lang_interact.opts.get('tests', [{}])
        if tests:
            result_prog['tests'] = [{}] * len(tests)
            result_interact['tests'] = [{}] * len(tests)
        is_shorted = False
        for i, test in enumerate(tests):
            test_name = test.get('name', 'test{:03d}'.format(i))
            if is_shorted:
                short_meta_defaults = {
                    'cg-mem': 0,
                    'cg-oom-killed': 0,
                    'csw-forced': 0,
                    'csw-voluntary': 0,
                    'exitcode': 0,
                    'exitsig': 0,
                    'exitsig-message': None,
                    'killed': False,
                    'max-rss': 0,
                    'message': None,
                    'status': 'SHORT_CIRCUIT',
                    'time': 0.0,
                    'time-wall': 0.0,
                }
                result_prog['tests'][i] = {
                    'name': test_name,
                    'meta': short_meta_defaults
                }
                result_interact['tests'][i] = {
                    'name': test_name,
                    'meta': short_meta_defaults
                }
                continue
            
            prog_retcode, prog_info, interact_retcode, interact_info = await self.execute(binary_prog, binary_interact, test)
            result_prog['tests'][i] = {
                'name': test_name,
                **prog_info
            }
            result_interact['tests'][i] = {
                'name': test_name,
                **interact_info
            }

            if prog_info['meta']['status'] in ['TIMED_OUT', 'RUNTIME_ERROR']:
                is_shorted = True

            if (prog_retcode != 0 or interact_retcode != 0) and (
                    test.get('fatal', False) or
                    self.lang_interact.opts.get('all_fatal', False)):
                break