*! version 1.0.0  30sep2026
*! pqsave: write Stata data to Parquet via Python/pyarrow (engine: pqtools.py)
/*
Syntax
    pqsave [varlist] [if] [in] [, options]        save the data in memory
    pqsave using filename [, options]             convert a .dta or .csv/.txt/.tsv
                                                  on disk (data in memory untouched)
Options
    name(string)        output file name; ".parquet" added if no .parquet/.pq
                        suffix. Default: name of the dataset in memory, or of
                        the using file
    directory(path)     output directory; default is the current directory
    replace             overwrite an existing file
    compression(str)    zstd (default), snappy, lz4, gzip, brotli, or none
    level(#)            compression level (zstd, gzip, brotli)
    chunksize(#)        rows per chunk / Parquet row group (default: ~256MB of
                        uncompressed data per chunk)
    tmpdir(path)        where the temporary .dta is written (default c(tmpdir));
                        use a fast local disk with room for a full copy
    nometa              do not store Stata metadata (labels, formats, notes...)
    nodates             leave %td/%tc variables as numbers instead of writing
                        Parquet date/timestamp columns
    delimiter(char)     csv only: field delimiter (default "," or tab for .tsv)
    viastata            csv only: parse with -import delimited- instead of pyarrow
    verbose             show progress and Python tracebacks

Notes
    Requires Stata 16+ with Python configured (-python set exec-) and pyarrow.
    Data in memory are handed to Python through a binary .dta: if the data are
    unchanged since -use- (and all rows are being written) the original file
    is read directly, otherwise a temporary copy is written with -save-.
*/
program define pqsave, rclass
    version 16
    syntax [varlist(default=none)] [if] [in] [using/] [,                    ///
        NAme(string) DIRectory(string) REPLACE                              ///
        COMPression(string) LEVel(integer -999) CHUNKsize(integer 0)        ///
        TMPdir(string) NOMETA NODATES DELIMiter(string) VIAStata VERbose ]

    local pq_ver "1.0.0"

    // ------------------------------------------------------------ source
    local mode "memory"
    if `"`using'"' != "" {
        if `"`varlist'`if'`in'"' != "" {
            di as err "varlist, if, and in may not be combined with using"
            exit 198
        }
        confirm file `"`using'"'
        mata: st_local("ext", strlower(pathsuffix(st_local("using"))))
        if "`ext'" == ".dta" local mode "dta"
        else if inlist("`ext'", ".csv", ".txt", ".tsv") local mode "csv"
        else {
            di as err "using file must be .dta, .csv, .txt, or .tsv"
            exit 198
        }
    }
    else if c(k) == 0 {
        di as err "no variables in memory"
        exit 111
    }
    if "`mode'" != "csv" & (`"`delimiter'"' != "" | "`viastata'" != "") {
        di as err "delimiter() and viastata are only allowed with a .csv using file"
        exit 198
    }

    // ------------------------------------------------------------ output
    if `"`name'"' == "" {
        if "`mode'" != "memory" local base `"`using'"'
        else {
            local base : char _dta[pq_source]
            if `"`base'"' == "" local base `"`c(filename)'"'
        }
        if `"`base'"' == "" {
            di as err "the data in memory have no file name; specify name()"
            exit 198
        }
        mata: st_local("name", pathrmsuffix(pathbasename(st_local("base"))))
    }
    if `"`directory'"' == "" local directory `"`c(pwd)'"'
    mata: st_local("ok", strofreal(direxists(st_local("directory"))))
    if !`ok' {
        di as err `"directory `directory' not found"'
        exit 601
    }
    mata: st_local("outfile", pathjoin(st_local("directory"), st_local("name")))
    mata: st_local("sfx", strlower(pathsuffix(st_local("outfile"))))
    if !inlist("`sfx'", ".parquet", ".pq") local outfile `"`outfile'.parquet"'
    capture confirm new file `"`outfile'"'
    if _rc {
        capture confirm file `"`outfile'"'
        if !_rc & "`replace'" == "" {
            di as err `"file `outfile' already exists; specify replace"'
            exit 602
        }
        if _rc {
            di as err `"cannot write `outfile'"'
            exit 603
        }
    }

    if `"`compression'"' == "" local compression "zstd"
    local compression = strlower(`"`compression'"')
    if !inlist(`"`compression'"', "zstd", "snappy", "lz4", "gzip", "brotli", "none") {
        di as err "compression() must be zstd, snappy, lz4, gzip, brotli, or none"
        exit 198
    }
    if `"`tmpdir'"' == "" local tmpdir `"`c(tmpdir)'"'

    _pq_python_init `pq_ver'

    // ------------------------------------------------------------ stage source
    local pq_src ""
    local pq_touse ""
    local pq_cols ""
    local made_tmp 0
    tempfile tmpf
    mata: st_local("tmpf", pathjoin(st_local("tmpdir"), pathbasename(st_local("tmpf")) + ".dta"))

    if "`mode'" == "memory" {
        local oldfn `"`c(filename)'"'
        local oldchanged = c(changed)
        if "`varlist'" == "" unab varlist : _all
        local pq_cols "`varlist'"

        // fast path: unchanged data read straight from the file they came from
        if `"`if'`in'"' == "" & `oldchanged' == 0 & `"`oldfn'"' != "" {
            capture confirm file `"`oldfn'"'
            if !_rc {
                capture dtaversion `"`oldfn'"'
                if !_rc & inrange(r(version), 117, 119) {
                    capture quietly describe using `"`oldfn'"', short
                    if !_rc & r(N) == _N & r(k) == c(k) local pq_src `"`oldfn'"'
                }
            }
        }
        if `"`pq_src'"' == "" {
            if `"`if'`in'"' != "" {
                tempvar touse
                mark `touse' `if' `in'
                local pq_touse "`touse'"
            }
            if "`verbose'" != "" di as txt "writing temporary .dta ..."
            quietly save `"`tmpf'"', emptyok
            local made_tmp 1
            local pq_src `"`tmpf'"'
            // -save- reset the dataset name and changed flag: put them back
            global S_FN `"`oldfn'"'
            if `oldchanged' {
                tempvar flip
                capture generate byte `flip' = .
                capture drop `flip'
            }
        }
        else if "`verbose'" != "" di as txt `"reading unchanged data directly from `pq_src'"'
        local pq_mode "dta"
        local pq_srcname : char _dta[pq_source]
        if `"`pq_srcname'"' == "" local pq_srcname `"`oldfn'"'
    }
    else if "`mode'" == "dta" {
        capture dtaversion `"`using'"'
        if !_rc & inrange(r(version), 117, 119) local pq_src `"`using'"'
        else {
            // pre-Stata-13 format: let Stata upgrade it in a scratch frame
            tempname fr
            frame create `fr'
            capture noisily frame `fr': quietly use `"`using'"'
            if !_rc capture noisily frame `fr': quietly save `"`tmpf'"', emptyok
            local rc = _rc
            frame drop `fr'
            if `rc' exit `rc'
            local made_tmp 1
            local pq_src `"`tmpf'"'
        }
        local pq_mode "dta"
        local pq_srcname `"`using'"'
    }
    else {
        if "`viastata'" != "" {
            if `"`delimiter'"' != "" local dopt `"delimiters(`"`delimiter'"')"'
            tempname fr
            frame create `fr'
            capture noisily frame `fr': quietly import delimited using `"`using'"', `dopt' clear
            if !_rc capture noisily frame `fr': quietly save `"`tmpf'"', emptyok
            local rc = _rc
            frame drop `fr'
            if `rc' exit `rc'
            local made_tmp 1
            local pq_src `"`tmpf'"'
            local pq_mode "dta"
            local pq_srcname `"`using'"'
        }
        else {
            local pq_src `"`using'"'
            local pq_mode "csv"
            if `"`delimiter'"' == "tab" local delimiter = char(9)
            local pq_delim `"`delimiter'"'
        }
    }

    // ------------------------------------------------------------ convert
    local pq_out `"`outfile'"'
    local pq_comp "`compression'"
    local pq_level = cond(`level' == -999, "", "`level'")
    local pq_chunk "`chunksize'"
    local pq_nometa "`nometa'"
    local pq_nodates "`nodates'"
    local pq_verbose "`verbose'"
    local pq_rc 1
    capture noisily python: pqtools.stata_save()
    local rc = _rc
    if `made_tmp' capture erase `"`tmpf'"'
    if `rc' exit `rc'
    if "`pq_rc'" != "0" exit 198

    local mb = `pq_r_bytes' / 2^20
    di as txt `"file `outfile' saved"' ///
        as txt " (" as res %15.0fc `pq_r_rows' as txt " rows, " ///
        as res `pq_r_cols' as txt " columns, " as res %9.1fc `mb' as txt " MB, " ///
        as res %6.1f `pq_r_seconds' as txt "s in Python)"
    return scalar N = `pq_r_rows'
    return scalar k = `pq_r_cols'
    return scalar bytes = `pq_r_bytes'
    return local filename `"`outfile'"'
end


program define _pq_python_init
    args ver
    capture findfile pqtools.py
    if _rc {
        di as err "pqtools.py not found on the ado-path; install it next to pqsave.ado"
        exit 601
    }
    local pq_py `"`r(fn)'"'
    local pq_ver "`ver'"
    capture python: import pyarrow
    if _rc {
        di as err "Python could not import pyarrow. Check -python query- and that"
        di as err "pyarrow is installed for the Python set with -python set exec-."
        exit 198
    }
    python: import sys, os, importlib; from sfi import Macro as _pqM; _pqd = os.path.dirname(os.path.abspath(_pqM.getLocal("pq_py"))); (_pqd in sys.path) or sys.path.insert(0, _pqd); import pqtools; pqtools = pqtools if pqtools.__version__ == _pqM.getLocal("pq_ver") else importlib.reload(pqtools)
end
