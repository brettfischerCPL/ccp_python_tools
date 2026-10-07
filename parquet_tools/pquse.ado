*! version 1.0.1  30sep2026
*! pquse: load a Parquet file into Stata via Python/pyarrow (engine: pqtools.py)
/*
Syntax
    pquse filename [, options]

Options
    directory(path)     directory holding filename (default: current directory,
                        or whatever path filename itself contains).
                        ".parquet" is appended if filename is not found as typed
    clear               replace the data in memory
    columns(list)       load only these columns (Parquet names, space separated;
                        quote names containing spaces)
    rows(#)             load only the first # rows
    chunksize(#)        rows per conversion chunk (default: ~256MB per chunk)
    tmpdir(path)        where the temporary .dta is written (default c(tmpdir))
    nometa              ignore Stata metadata stored by pqsave
    verbose             show progress and Python tracebacks

Notes
    Files written by pqsave come back with their storage types, formats,
    variable and value labels, notes/characteristics, dataset label, and sort
    order. Other Parquet files get the smallest Stata type that holds each
    column; names that are not valid in Stata are fixed up and the original
    name is kept as the variable label. Nested types (lists, structs, maps)
    are skipped with a warning.
*/
program define pquse, rclass
    version 16
    syntax anything(name=fname id="filename") [,                            ///
        DIRectory(string) CLEAR COLumns(string asis) ROWS(integer 0)        ///
        CHUNKsize(integer 0) TMPdir(string) NOMETA VERbose ]

    local pq_ver "1.0.1"
    local fname `fname'

    if `"`directory'"' != "" {
        mata: st_local("infile", pathjoin(st_local("directory"), st_local("fname")))
    }
    else local infile `"`fname'"'
    capture confirm file `"`infile'"'
    if _rc {
        mata: st_local("sfx", pathsuffix(st_local("infile")))
        if "`sfx'" == "" {
            capture confirm file `"`infile'.parquet"'
            if !_rc local infile `"`infile'.parquet"'
        }
    }
    confirm file `"`infile'"'
    mata: st_local("infile", pathresolve(pwd(), st_local("infile")))

    if c(changed) & "`clear'" == "" error 4
    if `"`tmpdir'"' == "" local tmpdir `"`c(tmpdir)'"'

    pqsetup, version(`pq_ver') quiet

    tempfile tmpf
    mata: st_local("tmpf", pathjoin(st_local("tmpdir"), pathbasename(st_local("tmpf")) + ".dta"))

    local pq_src `"`infile'"'
    local pq_out `"`tmpf'"'
    local pq_cols `"`columns'"'
    local pq_rows "`rows'"
    local pq_chunk "`chunksize'"
    local pq_nometa "`nometa'"
    local pq_verbose "`verbose'"
    local pq_rc 1
    capture noisily python: import pqtools; pqtools.stata_load()
    local rc = _rc
    if `rc' | "`pq_rc'" != "0" {
        capture erase `"`tmpf'"'
        exit `=cond(`rc', `rc', 198)'
    }

    capture noisily use `"`tmpf'"', clear
    local rc = _rc
    capture erase `"`tmpf'"'
    if `rc' exit `rc'
    // the data now "come from" the erased temp file; clear that name so a later
    // bare -save, replace- cannot write somewhere unexpected
    global S_FN ""

    di as txt "(" as res %15.0fc _N as txt " obs, " as res c(k) as txt " vars read; " ///
        as res %6.1f `pq_r_seconds' as txt "s in Python)"
    return scalar N = _N
    return scalar k = c(k)
    return local filename `"`infile'"'
end

