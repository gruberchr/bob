# Bob build tool
# Copyright (C) 2020  Jan Klötzke
#
# SPDX-License-Identifier: GPL-3.0-or-later

from . import BOB_INPUT_HASH
from .errors import ParseError
from .utils import escapePwsh, quotePwsh, isWindows, asHexStr
from .utils import joinScripts, sliceString
from base64 import b64encode
from enum import Enum
from glob import glob
from shlex import quote
from textwrap import dedent
import hashlib
import json
import os
import sys

__all__ = ['ScriptLanguage', 'getLanguage', 'StepSpec']

BASH_FINGERPRINT_SNIPPETS = [
    ("bob-libc-version", r"""
bob-libc-version()
{
    if ! type -p ${1:-${CC:-cc}} >/dev/null ; then
        echo "No C-Compiler!" >&2
        return 1
    fi

    # Machine type is important (e.g. x86_64)
    uname -m

    # Try glibc first
    cat >conftest.c <<EOF
#include <stdio.h>
#include <gnu/libc-version.h>
int main(){ printf("glibc %s\n", gnu_get_libc_version()); return 0; }
EOF
    if ${1:-${CC:-cc}} -o conftest conftest.c >/dev/null ; then
        ./conftest && return 0
    fi

    # Maybe musl libc? Link a simple program and extract runtime loader. On
    # musl the runtime loader is executable and outputs its version.
    cat >conftest.c <<EOF
int main(){ return 0; }
EOF
    if ! ${1:-${CC:-cc}} -o conftest conftest.c >/dev/null ; then
        echo "The C-Compiler does not seem to work... :(" >&2
        return 1
    fi

    DL=$(readelf -p .interp ./conftest | sed -n -e '/ld-musl/s/[^/]*\(\/.*\)/\1/p')
    if [[ -x $DL ]] ; then
        $DL 2>&1 || true
        return 0
    fi

    # Uhh?
    echo "Unsupported system. Please consider submitting your OS configuration for inclusion." >&2
    return 1
}
"""),

    ("bob-libstdc++-version", r"""
bob-libstdc++-version()
{
    if ! type -p ${1:-${CXX:-c++}} >/dev/null ; then
        echo "No C++-Compiler!" >&2
        return 1
    fi

    # Machine type is important (e.g. x86_64)
    uname -m

    cat >conftest.cpp <<EOF
#include <iostream>
int main(int /*argc*/, char ** /*argv*/)
{
    int ret = 1;
#ifdef __GLIBCXX__
    std::cout << "libstdc++ " << __GLIBCXX__ << &std::endl;
    ret = 0;
#endif
#ifdef _LIBCPP_VERSION
    std::cout << "libc++ " << _LIBCPP_VERSION << &std::endl;
    ret = 0;
#endif
    return ret;
}
EOF
    ${1:-${CXX:-c++}} -o conftest conftest.cpp >/dev/null
    ./conftest
}
"""),

    ("bob-hash-libraries", r"""
bob-hash-libraries()
{
    declare -a opts=( -o canary -xc - )
    local i

    for i in "$@" ; do
        opts+=( -l "$i" )
    done

    echo "int main(){return 0;}" | ${CC:-cc} "${opts[@]}"
    sha1sum $(ldd canary | grep -o -e '/[^[:space:]]*' | sort -u)
}
"""),
]


class ScriptLanguage(Enum):
    BASH = 'bash'
    PWSH = 'PowerShell'


class IncludeResolver:
    def __init__(self, fileLoader, baseDir, origText, sourceName, varBase):
        self.fileLoader = fileLoader
        self.baseDir = baseDir
        self.sourceName = sourceName
        self.varBase = varBase
        self.__incDigests = [ hashlib.sha1(origText.encode('utf8')).digest().hex() ]

    def __getitem__(self, item):
        mode = item[0]
        item = item[1:]
        content = []
        try:
            paths = sorted(glob(os.path.join(self.baseDir, item)))
            if not paths:
                raise ParseError("No files matched in include pattern '{}'!"
                    .format(item))
            for path in paths:
                content.append(self.fileLoader(path))
        except OSError as e:
            raise ParseError("Error including '"+item+"': " + str(e))
        content = b''.join(content)

        self.__incDigests.append(hashlib.sha1(content).digest().hex())
        if mode == '<':
            ret = self._includeFile(content)
        else:
            assert mode == "'"
            ret = self._includeLiteral(content)

        return ret

    def _includeFile(self, content):
        raise NotImplementedError()

    def _includeLiteral(self, content):
        raise NotImplementedError()

    def _resolveContent(self, result):
        raise NotImplementedError()

    def resolve(self, result):
        return (self._resolveContent(result), "\n".join(self.__incDigests))

# For each supported language the following runtime environments must be
# considered:
#
#  * Native POSIX platform
#  * Native Windows platform
#  * MSYS2 POSIX platform running on Windows
#
# When passing paths and/or writing them into scripts the paths may have to be
# converted.

class BashResolver(IncludeResolver):
    def __init__(self, fileLoader, baseDir, origText, sourceName, varBase):
        super().__init__(fileLoader, baseDir, origText, sourceName, varBase)
        self.prolog = []
        self.count = 0

    def _includeFile(self, content):
        var = "_{}{}".format(self.varBase, self.count)
        self.count += 1
        self.prolog.extend([
            "{VAR}=$(mktemp)".format(VAR=var),
            "_BOB_TMP_CLEANUP+=( ${VAR} )".format(VAR=var),
            "base64 -d > ${VAR} <<EOF".format(VAR=var)])
        self.prolog.extend(sliceString(b64encode(content).decode("ascii"), 76))
        self.prolog.append("EOF")
        return "${" + var + "}"

    def _includeLiteral(self, content):
        return quote(content.decode('utf8'))

    def _resolveContent(self, result):
        tail = ["_BOB_SOURCES[$LINENO]=" + quote(self.sourceName), result]
        return "\n".join(self.prolog + tail)


class BashLanguage:
    index = ScriptLanguage.BASH
    glue = "\ncd \"${BOB_CWD}\"\n"
    Resolver = BashResolver

    # When we try to execute bash scripts on Windows we have to take into
    # account that MSYS2 uses unix paths internally. So if we have
    #
    #   C:\foo\bar
    #
    # we have to transform this into
    #
    #  /c/foo/bar
    #
    if sys.platform == "win32":
        @staticmethod
        def __munge(p):
            if p[1:3] == ":\\":
                return "/" + p[0].lower() + "/" + p[3:].replace('\\', '/')
            else:
                return p.replace('\\', '/')
    else:
        @staticmethod
        def __munge(p):
            return p

    @staticmethod
    def __formatSetup(spec, keepEnv):
        env = { key: quote(value) for (key, value) in spec.env.items() }
        env.update({
            "PATH": ":".join(
                [quote(BashLanguage.__munge(os.path.abspath(p))) for p in spec.paths] +
                (["$PATH"] if not spec.hasSandbox else
                 [quote(p) for p in spec.sandboxPaths])
            ),
            "LD_LIBRARY_PATH": ":".join(
                quote(BashLanguage.__munge(os.path.abspath(p))) for p in spec.libraryPaths
            ),
            "BOB_CWD": quote(BashLanguage.__munge(os.path.abspath(spec.workspaceExecPath))),
        })

        ret = []

        if keepEnv:
            # Parse global config files if env should be kept
            ret.append(dedent("""\
                [[ -e /etc/bash.bashrc ]] && source /etc/bash.bashrc
                [[ -e ~/.bashrc ]] && source ~/.bashrc
                """))

        ret.extend([
            "# Special Bob array variables:",
            "declare -A BOB_ALL_PATHS=( {} )".format(" ".join(sorted(
                [ "[{}]={}".format(quote(name), quote(BashLanguage.__munge(os.path.abspath(path))))
                    for name,path in spec.allPaths ] ))),
            "declare -A BOB_DEP_PATHS=( {} )".format(" ".join(sorted(
                [ "[{}]={}".format(quote(name), quote(BashLanguage.__munge(os.path.abspath(path))))
                    for name,path in spec.depPaths ] ))),
            "declare -A BOB_TOOL_PATHS=( {} )".format(" ".join(sorted(
                [ "[{}]={}".format(quote(name), quote(BashLanguage.__munge(os.path.abspath(path))))
                    for name,path in spec.toolPaths ] ))),
            "",
            "# Environment:",
            "\n".join("export {}={}".format(k, v) for (k,v) in sorted(env.items()))
        ])
        return "\n".join(ret)

    @staticmethod
    def __formatScript(spec):
        colorize = not spec.isJenkins
        envFile = "/bob/env" if spec.hasSandbox else os.path.abspath(spec.envFile)
        ret = [
            BashLanguage.__formatSetup(spec, False),
            "",
            dedent("""\
                # Setup
                declare -p > {ENV_FILE}
                cd \"${{BOB_CWD}}\"
                """.format(ENV_FILE=BashLanguage.__munge(envFile))),
            dedent("""\
                # Error handling
                bob_handle_error()
                {
                    set +x"""),
            'echo "\x1b[31;1mStep failed with return status $1; Command:\x1b[0;31m ${BASH_COMMAND}\x1b[0m"' if colorize \
                else 'echo "Step failed with return status $1; Command: ${BASH_COMMAND}"',
            dedent("""\
                    echo "Call stack (most recent call first)"
                    i=0
                    while caller $i >/dev/null ; do
                            j=${BASH_LINENO[$i]}
                            while [[ $j -ge 0 && -z ${_BOB_SOURCES[$j]:+true} ]] ; do
                                    : $(( j-- ))
                            done
                            echo "    #$i: ${_BOB_SOURCES[$j]}, line $(( BASH_LINENO[$i] - j )), in ${FUNCNAME[$((i+1))]}"
                            : $(( i++ ))
                    done

                    exit $1
                }
                declare -A _BOB_SOURCES=( [0]="Bob prolog" )
                trap 'bob_handle_error $? >&2 ; exit 99' ERR
                trap 'for i in "${_BOB_TMP_CLEANUP[@]-}" ; do /bin/rm -f "$i" ; done' EXIT
                set -o errtrace -o nounset -o pipefail
                """),
            "",
            "# BEGIN BUILD SCRIPT",
            spec.script,
            "# END BUILD SCRIPT",
        ]
        return "\n".join(ret)

    @staticmethod
    def setupShell(spec, tmpDir, keepEnv):
        if spec.hasSandbox:
            execScriptFile = "/.script"
            realScriptFile = os.path.join(tmpDir, ".script")
        else:
            execScriptFile = os.path.join(tmpDir, "script")
            realScriptFile = execScriptFile

        with open(realScriptFile, "w") as f:
            f.write(BashLanguage.__formatSetup(spec, keepEnv))

        args = [ BashLanguage.__munge(os.path.abspath(a)) for a in spec.args ]
        return ["bash", "--rcfile", BashLanguage.__munge(execScriptFile), "-s", "--"] + args

    @staticmethod
    def setupCall(spec, tmpDir, keepEnv, trace):
        if spec.hasSandbox:
            execScriptFile = "/.script"
            realScriptFile = os.path.join(tmpDir, ".script")
        else:
            execScriptFile = os.path.join(tmpDir, "script")
            realScriptFile = execScriptFile

        with open(realScriptFile, "w") as f:
            f.write(BashLanguage.__formatScript(spec))

        ret = ["bash"]
        if trace: ret.append("-x")
        ret.extend(["--", BashLanguage.__munge(execScriptFile)])
        ret.extend(BashLanguage.__munge(os.path.abspath(a)) for a in spec.args)

        return ret

    @staticmethod
    def mangleFingerprints(scriptFragments, env):
        # join the script fragments first
        script = joinScripts(scriptFragments, BashLanguage.glue)

        # do not add preamble for empty scripts
        if not script: return ""

        # Add snippets as they match and a default settings preamble
        ret = [script]
        for n,s in BASH_FINGERPRINT_SNIPPETS:
            if n in script: ret.append(s)
        ret.extend(["set -o errexit", "set -o nounset", "set -o pipefail"])
        for n,v in sorted(env.items()):
            ret.append("export {}={}".format(n, quote(v)))
        return "\n".join(reversed(ret))

    @staticmethod
    def setupFingerprint(spec, env):
        env["BOB_CWD"] = BashLanguage.__munge(env["BOB_CWD"])
        return ["bash", "-x", "-c", spec.fingerprintScript]


class PwshResolver(IncludeResolver):
    def __init__(self, fileLoader, baseDir, origText, sourceName, varBase):
        super().__init__(fileLoader, baseDir, origText, sourceName, varBase)
        self.prolog = []
        self.count = 0

    def _includeFile(self, content):
        var = "$_{}{}".format(self.varBase, self.count)
        self.count += 1
        self.prolog.extend([
            "{VAR} = (New-TemporaryFile).FullName".format(VAR=var),
            "$_BOB_TMP_CLEANUP += {VAR}".format(VAR=var),
            "[Convert]::FromBase64String(@'"])
        self.prolog.extend(sliceString(b64encode(content).decode("ascii"), 76))
        self.prolog.append("'@) | Set-Content {VAR} -AsByteStream".format(VAR=var))
        return var

    def _includeLiteral(self, content):
        return quotePwsh(content.decode('utf8'))

    def _resolveContent(self, result):
        return "\n".join(self.prolog + [result])


class PwshLanguage:
    index = ScriptLanguage.PWSH
    glue = "\ncd $Env:BOB_CWD\n"
    Resolver = PwshResolver

    @staticmethod
    def __formatSetup(spec):
        pathSep = ";" if sys.platform == "win32" else ":"
        env = { key: escapePwsh(value) for (key, value) in spec.env.items() }
        env.update({
            "PATH": pathSep.join(
                [escapePwsh(os.path.abspath(p)) for p in spec.paths] +
                (["$Env:PATH"] if not spec.hasSandbox else
                 [escapePwsh(p) for p in spec.sandboxPaths])
            ),
            "LD_LIBRARY_PATH": pathSep.join(
                escapePwsh(os.path.abspath(p)) for p in spec.libraryPaths
            ),
            "BOB_CWD": escapePwsh(os.path.abspath(spec.workspaceExecPath)),
        })

        ret = [
            "# Special Bob array variables:",
            "$BOB_ALL_PATHS=@{{ {} }}".format("; ".join(sorted(
                [ '{} = "{}"'.format(quotePwsh(name), escapePwsh(os.path.abspath(path)))
                    for name,path in spec.allPaths ] ))),
            "$BOB_DEP_PATHS=@{{ {} }}".format("; ".join(sorted(
                [ '{} = "{}"'.format(quotePwsh(name), escapePwsh(os.path.abspath(path)))
                    for name,path in spec.depPaths ] ))),
            "$BOB_TOOL_PATHS=@{{ {} }}".format("; ".join(sorted(
                [ '{} = "{}"'.format(quotePwsh(name), escapePwsh(os.path.abspath(path)))
                    for name,path in spec.toolPaths ] ))),
            "",
            "# Environment:",
            "\n".join('$Env:{}="{}"'.format(k, v) for (k,v) in sorted(env.items()))
        ]
        return "\n".join(ret)

    @staticmethod
    def __formatScript(spec, trace):
        envFile = "/bob/env" if spec.hasSandbox else os.path.abspath(spec.envFile)
        ret = [
            PwshLanguage.__formatSetup(spec),
            "",
            dedent("""\
                # Setup
                Get-ChildItem Env: | Select-Object Name,Value | Export-Csv {ENV_FILE}
                cd $Env:BOB_CWD
                """.format(ENV_FILE=envFile)),
            dedent("""\
                # Error handling
                $ErrorActionPreference="Stop"
                Set-PSDebug -Strict
                """),
            "",
            dedent("""\
                try {
                    $_BOB_TMP_CLEANUP = @()
                """),
            "# BEGIN BUILD SCRIPT",
            spec.script,
            "# END BUILD SCRIPT",
            dedent("""\
                } finally {
                    foreach($f in $_BOB_TMP_CLEANUP) {
                        Remove-Item $f -Force
                    }
                }"""),
        ]
        return "\n".join(ret)

    @staticmethod
    def setupShell(spec, tmpDir, keepEnv):
        if spec.hasSandbox:
            execScriptFile = "/.script.ps1"
            realScriptFile = os.path.join(tmpDir, ".script.ps1")
        else:
            execScriptFile = os.path.join(tmpDir, "script.ps1")
            realScriptFile = execScriptFile

        with open(realScriptFile, "w") as f:
            f.write(PwshLanguage.__formatSetup(spec))

        interpreter = "powershell" if isWindows() else "pwsh"
        args = [ os.path.abspath(a) for a in spec.args ]
        return [interpreter, "-ExecutionPolicy", "Bypass", "-NoExit", "-File",
            execScriptFile] + args

    @staticmethod
    def setupCall(spec, tmpDir, keepEnv, trace):
        if spec.hasSandbox:
            execScriptFile = "/.script.ps1"
            realScriptFile = os.path.join(tmpDir, ".script.ps1")
        else:
            execScriptFile = os.path.join(tmpDir, "script.ps1")
            realScriptFile = execScriptFile

        with open(realScriptFile, "w") as f:
            f.write(PwshLanguage.__formatScript(spec, trace))

        interpreter = "powershell" if isWindows() else "pwsh"
        ret = [interpreter, "-ExecutionPolicy", "Bypass", "-File", execScriptFile]
        ret.extend(os.path.abspath(a) for a in spec.args)

        return ret

    @staticmethod
    def mangleFingerprints(scriptFragments, env):
        # join the script fragments first
        script = joinScripts(scriptFragments, PwshLanguage.glue)

        # do not add preamble for empty scripts
        if not script: return ""

        # Add snippets as they match and a default settings preamble
        ret = [script]
        ret.extend(['$ErrorActionPreference="Stop"', 'Set-PSDebug -Strict'])
        for n,v in sorted(env.items()):
            ret.append('$Env:{}="{}"'.format(k, escapePwsh(v)))

        return "\n".join(reversed(ret))

    @staticmethod
    def setupFingerprint(spec, env):
        interpreter = "powershell" if isWindows() else "pwsh"
        return [interpreter, "-c", spec.fingerprintScript]


LANG = {
    ScriptLanguage.BASH : BashLanguage,
    ScriptLanguage.PWSH : PwshLanguage,
}

def getLanguage(language):
    return LANG[language]


class StepSpec:

    @classmethod
    def fromStep(cls, step, envFile, envWhiteList, logFile=None, isJenkins=False):
        self = cls()
        scriptLanguage = step.getPackage().getRecipe().scriptLanguage
        self.__data = d = {
            'envFile' : envFile,
            'envWhiteList' : list(envWhiteList),
            'logFile' : logFile,
            'isJenkins' : isJenkins,
            'vsn' : asHexStr(BOB_INPUT_HASH),
            'language' : scriptLanguage.index.value,
            'env' : dict(step.getEnv()),
            'paths' : step.getPaths(),
            'libraryPaths' : step.getLibraryPaths(),
            'workspace' : (step.getWorkspacePath(), step.getExecPath()),
            'args' : [ a.getExecPath(step) for a in step.getArguments() ],
            'allPaths' : sorted([
                (a.getPackage().getName(), a.getExecPath(step))
                for a in step.getAllDepSteps()
            ]),
            'depPaths' : sorted([
                (a.getPackage().getName(), a.getExecPath(step))
                for a in step.getArguments() if a.isValid()
            ]),
            'toolPaths' : sorted([
                (n, os.path.join(t.getStep().getExecPath(step), t.getPath()))
                for (n,t) in step.getTools().items()
            ]),
        }

        if step.isCheckoutStep():
            d['clean'] = False
        elif step.isPackageStep():
            d['clean'] = True
        else:
            d['clean'] = None

        # fetch sandbox if configured
        if step.getSandbox() is not None:
            d['sandbox'] = s = {
                'root' : step.getSandbox().getStep().getWorkspacePath(),
                'paths' : step.getSandbox().getPaths(),
                'hostMounts' : step.getSandbox().getMounts(),
                'netAccess' : step.hasNetAccess(),
                'depMounts' : [
                    (dep.getWorkspacePath(), dep.getExecPath(step))
                    for dep in step.getAllDepSteps() if dep.isValid()
                ],
            }

            # special handling to mount all previous steps of current package
            extra = step
            while extra.isValid() and len(extra.getArguments()) > 0:
                extra = extra.getArguments()[0]
                if extra.isValid():
                    s['depMounts'].append((extra.getWorkspacePath(), extra.getExecPath(step)))

        d['preRunCmds'] = step.getJenkinsPreRunCmds() if isJenkins else step.getPreRunCmds()
        d['script'] = step.getJenkinsScript() if isJenkins else step.getScript()
        d['postRunCmds'] = step.getJenkinsPostRunCmds() if isJenkins else step.getPostRunCmds()
        d['fingerprintScript'] = step._getFingerprintScript()

        return self

    @classmethod
    def fromFile(cls, f):
        d = json.load(f)
        if d.get('vsn') != asHexStr(BOB_INPUT_HASH):
            raise ParseError("The spec file was created by a different Bob version and is incompatible",
                help="Please re-run this step via bob to fix this error.")
        self = cls()
        self.__data = d
        return self

    def toFile(self, f):
        json.dump(self.__data, f, indent="\t", sort_keys=True)

    def toString(self):
        return json.dumps(self.__data, indent="\t", sort_keys=True)

    @property
    def hasSandbox(self):
        return 'sandbox' in self.__data

    @property
    def language(self):
        return getLanguage(ScriptLanguage(self.__data['language']))

    @property
    def workspaceWorkspacePath(self):
        return self.__data['workspace'][0]

    @property
    def workspaceExecPath(self):
        return self.__data['workspace'][1]

    @property
    def args(self):
        return self.__data['args']

    @property
    def env(self):
        return self.__data['env']

    @property
    def paths(self):
        return self.__data['paths']

    @property
    def sandboxDepMounts(self):
        return self.__data['sandbox']['depMounts']

    @property
    def sandboxNetAccess(self):
        return self.__data['sandbox']['netAccess']

    @property
    def sandboxRootWorkspace(self):
        return self.__data['sandbox']['root']

    @property
    def sandboxHostMounts(self):
        return self.__data['sandbox']['hostMounts']

    @property
    def sandboxPaths(self):
        return self.__data['sandbox']['paths']

    @property
    def envWhiteList(self):
        return set(self.__data['envWhiteList'])

    @property
    def envFile(self):
        return self.__data['envFile']

    @property
    def isJenkins(self):
        return self.__data['isJenkins']

    @property
    def clean(self):
        return self.__data['clean']

    @property
    def logFile(self):
        return self.__data['logFile']

    @property
    def preRunCmds(self):
        return self.__data['preRunCmds']

    @property
    def script(self):
        return self.__data['script']

    @property
    def fingerprintScript(self):
        return self.__data['fingerprintScript']

    @property
    def postRunCmds(self):
        return self.__data['postRunCmds']

    @property
    def libraryPaths(self):
        return self.__data['libraryPaths']

    @property
    def allPaths(self):
        return self.__data['allPaths']

    @property
    def depPaths(self):
        return self.__data['depPaths']

    @property
    def toolPaths(self):
        return self.__data['toolPaths']
