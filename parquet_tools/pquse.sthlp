{smcl}
{* *! version 1.0.0  06oct2026}{...}
{vieweralsosee "pqsave" "help pqsave"}{...}
{vieweralsosee "pqsetup" "help pqsetup"}{...}
{vieweralsosee "[D] use" "help use"}{...}
{vieweralsosee "[P] PyStata integration" "help python"}{...}
{viewerjumpto "Syntax" "pquse##syntax"}{...}
{viewerjumpto "Description" "pquse##description"}{...}
{viewerjumpto "Options" "pquse##options"}{...}
{viewerjumpto "Remarks" "pquse##remarks"}{...}
{viewerjumpto "Examples" "pquse##examples"}{...}
{viewerjumpto "Stored results" "pquse##results"}{...}
{viewerjumpto "Author" "pquse##author"}{...}
{title:Title}

{phang}
{bf:pquse} {hline 2} Load a Parquet file into Stata using Python and pyarrow


{marker syntax}{...}
{title:Syntax}

{p 8 17 2}
{cmd:pquse}
{it:{help filename}}
[{cmd:,} {it:options}]

{pstd}
If {it:filename} is not found as typed and has no extension, {cmd:.parquet} is
appended. Enclose {it:filename} in double quotes if it contains spaces.

{synoptset 24 tabbed}{...}
{synopthdr}
{synoptline}
{syntab:Main}
{synopt:{opt dir:ectory(path)}}directory containing {it:filename}{p_end}
{synopt:{opt clear}}replace the data in memory, even if they have changed{p_end}

{syntab:Subsetting}
{synopt:{opt col:umns(list)}}load only these columns{p_end}
{synopt:{opt rows(#)}}load only the first {it:#} rows{p_end}

{syntab:Performance}
{synopt:{opt chunk:size(#)}}rows converted at a time; default is about 256 MB of
data{p_end}
{synopt:{opt tmp:dir(path)}}directory for the temporary .dta; default is {cmd:c(tmpdir)}{p_end}

{syntab:Content}
{synopt:{opt nometa}}ignore Stata metadata stored by {cmd:pqsave}{p_end}

{syntab:Reporting}
{synopt:{opt ver:bose}}show progress messages and full Python error tracebacks{p_end}
{synoptline}
{p2colreset}{...}


{marker description}{...}
{title:Description}

{pstd}
{cmd:pquse} loads an Apache Parquet file into memory. It is designed for very
large files, where Stata's built-in Parquet routines are slow.

{pstd}
Python streams the Parquet file in chunks and writes a temporary .dta file,
which Stata then reads with {helpb use}. Memory used on the Python side is
bounded by the chunk size; Stata needs enough memory to hold the loaded data.

{pstd}
Files written by {helpb pqsave} come back as they were saved: storage types,
display formats, variable labels, value labels, notes and other
characteristics, the dataset label, and the sort order. Parquet files from
other sources (R, Python, DuckDB, Spark, and so on) are also supported; see
{it:{help pquse##foreign:Files not written by pqsave}}.

{pstd}
{cmd:pquse} requires Stata 16 or newer and a Python installation set with
{helpb python:python set exec}. The Python packages it needs are installed
automatically if missing; see {helpb pqsetup}.


{marker options}{...}
{title:Options}

{dlgtab:Main}

{phang}
{opt directory(path)} specifies the directory containing {it:filename}. By
default, {it:filename} is interpreted relative to the current directory, and it
may include its own path.

{phang}
{opt clear} permits the data in memory to be replaced even if they have changed
since last saved. Without {cmd:clear}, {cmd:pquse} stops before doing any work if
the data in memory have unsaved changes.

{dlgtab:Subsetting}

{phang}
{opt columns(list)} loads only the listed columns, in the order listed. Use the
names as they appear in the Parquet file, separated by spaces; enclose names
containing spaces in double quotes. Because Parquet stores each column
separately, columns that are not requested are never read, which can save a
great deal of time.

{phang}
{opt rows(#)} loads only the first {it:#} rows. This is useful for inspecting a
large file. The default, 0, loads all rows.

{dlgtab:Performance}

{phang}
{opt chunksize(#)} sets how many rows are converted at a time. The default
targets about 256 MB of data per chunk, between 20,000 and 4,000,000 rows.

{phang}
{opt tmpdir(path)} sets where the temporary .dta is written. It needs room for
a full uncompressed copy of the loaded data, which can be several times the
size of the Parquet file. A fast local disk is recommended.

{dlgtab:Content}

{phang}
{opt nometa} ignores any Stata metadata in the file, so the data load as they
would from a file written by another program.

{dlgtab:Reporting}

{phang}
{opt verbose} reports progress after each chunk and, if an error occurs,
displays the full Python traceback.


{marker remarks}{...}
{title:Remarks}

{pstd}
Remarks are presented under the following headings:

{phang2}{help pquse##foreign:Files not written by pqsave}{p_end}
{phang2}{help pquse##names:Variable names}{p_end}
{phang2}{help pquse##after:After loading}{p_end}
{phang2}{help pquse##limits:Limitations}{p_end}

{marker foreign}{...}
{title:Files not written by pqsave}

{pstd}
Each column is given the smallest Stata storage type that holds all of its
values. Integer ranges are taken from the file's statistics when available;
otherwise, and for string columns, {cmd:pquse} first reads those columns once
to find their ranges and lengths.

{p2colset 9 36 38 2}{...}
{p2col:Parquet (Arrow) type}Stata type{p_end}
{p2line}
{p2col:integers, boolean}{cmd:byte}, {cmd:int}, {cmd:long}, or {cmd:double}{p_end}
{p2col:{cmd:float32}, {cmd:float16}}{cmd:float}{p_end}
{p2col:{cmd:float64}, {cmd:decimal}}{cmd:double}{p_end}
{p2col:strings, binary, dictionary}{cmd:str}{it:#}, or {cmd:strL} if longer than
2,045 bytes{p_end}
{p2col:{cmd:date32}, {cmd:date64}}{cmd:long} with {cmd:%td} format{p_end}
{p2col:{cmd:timestamp}}{cmd:double} with {cmd:%tc} format{p_end}
{p2col:{cmd:time32}, {cmd:time64}}{cmd:double} with {cmd:%tcHH:MM:SS.sss} format{p_end}
{p2col:{cmd:duration}}{cmd:double} (in the column's own units){p_end}
{p2col:lists, structs, maps}skipped, with a warning{p_end}
{p2line}
{p2colreset}{...}

{pstd}
Nulls become missing values ({cmd:.}) in numeric variables and empty strings
in string variables. {cmd:NaN} and infinite values become {cmd:.}.

{pstd}
Timestamps with a time zone are loaded as UTC clock time, and a note is
displayed. Integers whose absolute value exceeds 2^53 are stored as
{cmd:double} and lose precision, with a warning.

{marker names}{...}
{title:Variable names}

{pstd}
Column names that are not valid Stata names are changed: invalid characters
become underscores, names beginning with a digit or matching a reserved word get
a leading underscore, names are truncated to 32 characters, and duplicates are
numbered. The original name is kept as the variable label. For example, columns
{cmd:my var} and {cmd:2nd} become {cmd:my_var} and {cmd:_2nd}.

{marker after}{...}
{title:After loading}

{pstd}
The full path of the Parquet file is stored in the characteristic
{cmd:_dta[pq_source]}. {helpb pqsave} uses it to name the output file by
default, and does not save it into the new file.

{pstd}
Because the data were read from a temporary file that has since been erased,
the dataset has no file name afterward. Use {cmd:save} {it:filename} to save
it as a .dta.

{marker limits}{...}
{title:Limitations}

{phang2}
o Partitioned Parquet datasets (a directory of files) are not supported; load
one file at a time.{p_end}

{phang2}
o Rows cannot be filtered on values while loading; use {opt rows()} or drop
observations after loading.{p_end}

{phang2}
o The number of variables is limited by your Stata edition (2,048 for
Stata/BE, 32,767 for Stata/SE).{p_end}

{phang2}
o {cmd:strL} variables are converted more slowly than other types.{p_end}


{marker examples}{...}
{title:Examples}

{pstd}Load {cmd:auto.parquet} from the current directory{p_end}
{phang2}{cmd:. pquse auto, clear}{p_end}

{pstd}Load from another directory{p_end}
{phang2}{cmd:. pquse panel_2024, directory("D:/data/parquet") clear}{p_end}

{pstd}Look at the first 1,000 rows of a few columns{p_end}
{phang2}{cmd:. pquse panel_2024, dir("D:/data/parquet") columns(id year balance) rows(1000) clear}{p_end}

{pstd}Load a file from another program, ignoring any stored metadata{p_end}
{phang2}{cmd:. pquse "D:/shared/from python.parquet", nometa clear}{p_end}

{pstd}Round trip{p_end}
{phang2}{cmd:. sysuse auto, clear}{p_end}
{phang2}{cmd:. pqsave, replace}{p_end}
{phang2}{cmd:. pquse auto, clear}{p_end}
{phang2}{cmd:. describe}{p_end}


{marker results}{...}
{title:Stored results}

{pstd}
{cmd:pquse} stores the following in {cmd:r()}:

{synoptset 20 tabbed}{...}
{p2col 5 20 24 2: Scalars}{p_end}
{synopt:{cmd:r(N)}}number of observations loaded{p_end}
{synopt:{cmd:r(k)}}number of variables loaded{p_end}

{p2col 5 20 24 2: Macros}{p_end}
{synopt:{cmd:r(filename)}}full path of the Parquet file{p_end}
{p2colreset}{...}


{marker author}{...}
{title:Author}

{pstd}
California Policy Lab. Source code is maintained in CPL's OneDev repository on
the secure server.{p_end}


{title:Also see}

{psee}
Help: {helpb pqsave}, {helpb pqsetup}, {helpb use}, {helpb python}
{p_end}
