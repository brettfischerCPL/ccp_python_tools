*! version 1.0.0  07oct2026
*! pqsetup: check (and if needed install) the Python packages used by pqsave/pquse
/*
Syntax
    pqsetup [, wheelhouse(path) noinstall force]

    Run by pqsave and pquse automatically. Run it yourself to see which
    versions are in use, or with -force- to re-check after changing Python.

Options
    wheelhouse(path)  folder of .whl files to install from (offline). Default:
                      $PQ_WHEELHOUSE if set, otherwise S:/python/packages
    noinstall         report missing/outdated packages but do not install
    force             re-check even if already checked this session

Minimum versions are set in the locals numpy_min and pyarrow_min below.
*/
program define pqsetup
    version 16
    syntax [, WHEELhouse(string) NOINSTALL FORCE VERsion(string) QUIET]

    local numpy_min   "1.21"
    local pyarrow_min "12.0"

    if `"`wheelhouse'"' == "" local wheelhouse `"$PQ_WHEELHOUSE"'
    if `"`wheelhouse'"' == "" local wheelhouse "S:/python/packages"

    // ------------------------------------------------------- Python present?
    capture quietly python query
    if _rc {
        di as err "Stata cannot find a Python installation; see {help python:help python}"
        di as err "and set one with -python set exec-."
        exit 198
    }
    local pypath `"`r(execpath)'"'
    if `"`pypath'"' == "" python: _pq_exe()

    // ------------------------------------------------------- dependencies
    if "$PQ_DEPS_OK" != "1" | "`force'" != "" {
        python: _pq_depcheck()
        if "`need'" != "" {
            foreach p of local need {
                if "``p'_ver'" == "" di as txt "`p' is not installed"
                else di as txt "`p' ``p'_ver' is older than the required ``p'_min'"
            }
            if "`noinstall'" != "" {
                di as err "required Python packages are missing or out of date"
                exit 198
            }
            mata: st_local("ok", strofreal(direxists(st_local("wheelhouse"))))
            if !`ok' {
                di as err `"package folder `wheelhouse' not found; specify wheelhouse()"'
                di as err "or set the global PQ_WHEELHOUSE"
                exit 601
            }
            di as txt `"installing `need' from `wheelhouse' ..."'
            shell "`pypath'" -m pip install --no-index --find-links="`wheelhouse'" --upgrade `need'

            python: _pq_depcheck()
            if "`need'" != "" {
                if "`stale'" != "" {
                    di as err "`stale' was upgraded, but this Stata session already loaded"
                    di as err "the old version. Restart Stata, then rerun your command."
                    exit 198
                }
                di as err "installation did not succeed. To install by hand, run in Stata:"
                di as err `"  shell "`pypath'" -m pip install --no-index --find-links="`wheelhouse'" --upgrade `need'"'
                di as err "If pip reports a permissions error, add --user to that command."
                exit 198
            }
            di as txt "installed: numpy `numpy_ver', pyarrow `pyarrow_ver'"
        }
        global PQ_DEPS_OK 1
    }

    // ------------------------------------------------------- engine module
    capture findfile pqtools.py
    if _rc {
        di as err "pqtools.py not found on the ado-path; install it next to pqsave.ado"
        exit 601
    }
    local pq_py `"`r(fn)'"'
    local pq_ver "`version'"
    python: _pq_import()

    if "`quiet'" == "" {
        python: _pq_depcheck()
        di as txt "Python:   " as res `"`pypath'"'
        di as txt "numpy:    " as res "`numpy_ver'" as txt " (need `numpy_min'+)"
        di as txt "pyarrow:  " as res "`pyarrow_ver'" as txt " (need `pyarrow_min'+)"
        di as txt "pqtools:  " as res `"`pq_py'"'
    }
end

version 16
python:
def _pq_depcheck():
    """Set locals need/stale/numpy_ver/pyarrow_ver in the calling program."""
    import importlib, re, sys
    from sfi import Macro
    importlib.invalidate_caches()

    def ver(s):
        n = [int(x) for x in re.findall(r"\d+", s)[:3]]
        return tuple(n + [0] * (3 - len(n)))

    need, stale = [], []
    for mod in ("numpy", "pyarrow"):
        was_loaded = mod in sys.modules
        try:
            v = importlib.import_module(mod).__version__
        except Exception:
            v = ""
        Macro.setLocal(mod + "_ver", v)
        if not v or ver(v) < ver(Macro.getLocal(mod + "_min")):
            need.append(mod)
            if was_loaded and v:
                stale.append(mod)
    Macro.setLocal("need", " ".join(need))
    Macro.setLocal("stale", " ".join(stale))


def _pq_exe():
    """Fallback location of python(.exe) when -python query- does not report it
    (sys.executable is Stata itself inside embedded Python)."""
    import os, sys
    from sfi import Macro
    for d in (sys.exec_prefix, sys.base_exec_prefix):
        for name in ("python.exe", os.path.join("bin", "python3"), os.path.join("bin", "python")):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                Macro.setLocal("pypath", p)
                return


def _pq_import():
    """Import pqtools.py from the ado-path (reloading it if its version changed)."""
    import importlib, os, sys
    import __main__
    from sfi import Macro
    d = os.path.dirname(os.path.abspath(Macro.getLocal("pq_py")))
    if d not in sys.path:
        sys.path.insert(0, d)
    import pqtools
    want = Macro.getLocal("pq_ver")
    if want and pqtools.__version__ != want:
        pqtools = importlib.reload(pqtools)
    __main__.pqtools = pqtools
end
