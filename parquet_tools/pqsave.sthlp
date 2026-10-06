{smcl}
{* *! version 1.0.0  06oct2026}{...}
{vieweralsosee "pquse" "help pquse"}{...}
{vieweralsosee "[D] save" "help save"}{...}
{vieweralsosee "[P] PyStata integration" "help python"}{...}
{viewerjumpto "Syntax" "pqsave##syntax"}{...}
{viewerjumpto "Description" "pqsave##description"}{...}
{viewerjumpto "Options" "pqsave##options"}{...}
{viewerjumpto "Remarks" "pqsave##remarks"}{...}
{viewerjumpto "Examples" "pqsave##examples"}{...}
{viewerjumpto "Stored results" "pqsave##results"}{...}
{viewerjumpto "Author" "pqsave##author"}{...}
{title:Title}

{phang}
{bf:pqsave} {hline 2} Save data to a Parquet file using Python and pyarrow


{marker syntax}{...}
{title:Syntax}

{pstd}
Save the data in memory

{p 8 17 2}
{cmd:pqsave}
[{varlist}]
{ifin}
[{cmd:,} {it:options}]

{pstd}
Convert a .dta or delimited text file on disk (the data in memory are not changed)

{p 8 17 2}
{cmd:pqsave}
{cmd:using} {it:{help filename}}
[{cmd:,} {it:options}]

{pstd}
{it:filename} must end in {cmd:.dta}, {cmd:.csv}, {cmd:.txt}, or {cmd:.tsv}.

{synoptset 24 tabbed}{...}
{synopthdr}
{synoptline}
{syntab:Output}
{synopt:{opt na:me(filename)}}name of the Parquet file; default is the name of the
dataset in memory or of the {cmd:using} file{p_end}
{synopt:{opt dir:ectory(path)}}directory for the Parquet file; default is the current
directory{p_end}
{synopt:{opt replace}}overwrite an existing file{p_end}

{syntab:Performance}
{synopt:{opt comp:ression(method)}}{cmd:zstd} (default), {cmd:snappy}, {cmd:lz4},
{cmd:gzip}, {cmd:brotli}, or {cmd:none}{p_end}
{synopt:{opt lev:el(#)}}compression level for {cmd:zstd}, {cmd:gzip}, or {cmd:brotli}{p_end}
{synopt:{opt chunk:size(#)}}rows per chunk and Parquet row group; default is about
256 MB of uncompressed data{p_end}
{synopt:{opt tmp:dir(path)}}directory for the temporary .dta; default is {cmd:c(tmpdir)}{p_end}

{syntab:Content}
{synopt:{opt nometa}}do not store Stata metadata in the file{p_end}
{synopt:{opt nodates}}write {cmd:%td} and {cmd:%tc} variables as plain numbers{p_end}

{syntab:Delimited files only}
{synopt:{opt delim:iter(char)}}field delimiter; default is {cmd:","}, or tab for
{cmd:.tsv}; {cmd:delimiter(tab)} is allowed{p_end}
{synopt:{opt vias:tata}}parse the file with {helpb import delimited} instead of
Python{p_end}

{syntab:Reporting}
{synopt:{opt ver:bose}}show progress messages and full Python error tracebacks{p_end}
{synoptline}
{p2colreset}{...}


{marker description}{...}
{title:Description}

{pstd}
{cmd:pqsave} writes Stata data to an Apache Parquet file. It is designed for very
large datasets (hundreds of millions of rows and hundreds of variables), where
Stata's built-in Parquet routines are slow.

{pstd}
Data never pass through Stata's cell-by-cell Python interface. Instead, Stata
writes (or already has) a binary .dta file, and Python reads that file directly
as a memory-mapped array, converting it to Parquet in vectorized chunks. Memory
used on the Python side is bounded by the chunk size, not the size of the data.

{pstd}
Variable labels, value labels, storage types, display formats, notes and other
characteristics, the dataset label, and the sort order are stored in the file
and restored by {helpb pquse}. Programs other than {cmd:pquse} ignore them.

{pstd}
{cmd:pqsave} requires Stata 16 or newer, a Python installation set with
{helpb python:python set exec}, and the Python package {cmd:pyarrow}.


{marker options}{...}
{title:Options}

{dlgtab:Output}

{phang}
{opt name(filename)} names the output file. If {it:filename} does not end in
{cmd:.parquet} or {cmd:.pq}, {cmd:.parquet} is appended. By default the name is
taken from, in order: the {cmd:using} file; the file the data were loaded from
with {cmd:pquse}; or the file the data were last loaded from or saved to
({cmd:c(filename)}). If none exists, {cmd:name()} is required.

{phang}
{opt directory(path)} specifies the output directory. The default is the
current working directory. The directory must already exist.

{phang}
{opt replace} permits {cmd:pqsave} to overwrite an existing file. The new file
is written under a temporary name and renamed only after it is complete, so a
failed save leaves any existing file intact.

{dlgtab:Performance}

{phang}
{opt compression(method)} sets the Parquet compression codec. {cmd:zstd} (the
default) gives small files at high speed. {cmd:snappy} and {cmd:lz4} are
slightly faster and produce somewhat larger files. {cmd:gzip} and {cmd:brotli}
are slow to write. {cmd:none} disables compression.

{phang}
{opt level(#)} sets the compression level for {cmd:zstd}, {cmd:gzip}, or
{cmd:brotli}. The default is the library's default (level 1 for {cmd:zstd}).
Higher levels give smaller files and longer run times.

{phang}
{opt chunksize(#)} sets how many rows are converted at a time; each chunk
becomes one Parquet row group. The default targets about 256 MB of uncompressed
data per chunk, between 20,000 and 4,000,000 rows. Smaller chunks reduce Python's
memory use; larger chunks can compress slightly better.

{phang}
{opt tmpdir(path)} sets where the temporary .dta is written when one is needed
(see {it:{help pqsave##remarks:Remarks}}). It needs room for a full copy of the
data. Pointing it at a fast local disk is usually the single largest
improvement in run time.

{dlgtab:Content}

{phang}
{opt nometa} omits the Stata metadata. The Parquet columns and their types are
unaffected.

{phang}
{opt nodates} leaves variables with {cmd:%td} and {cmd:%tc} formats as numbers.
By default they are written as Parquet {cmd:date32} and {cmd:timestamp[ms]}
columns so that R, Python, DuckDB, Spark, and similar tools read them as dates.
{cmd:pquse} converts them back either way.

{dlgtab:Delimited files only}

{phang}
{opt delimiter(char)} specifies the field delimiter of a {cmd:using} text file.
The default is a comma, or a tab for files ending in {cmd:.tsv}. Type
{cmd:delimiter(tab)} for tab-delimited files with other extensions.

{phang}
{opt viastata} reads the file with {helpb import delimited} in a temporary frame
and then converts it as a .dta. Use it when you want Stata's type detection or
when Python's reader fails. It requires enough memory to hold the file in Stata.

{dlgtab:Reporting}

{phang}
{opt verbose} reports progress after each chunk and, if an error occurs,
displays the full Python traceback.


{marker remarks}{...}
{title:Remarks}

{pstd}
Remarks are presented under the following headings:

{phang2}{help pqsave##how:How pqsave reads your data}{p_end}
{phang2}{help pqsave##types:Storage types}{p_end}
{phang2}{help pqsave##csv:Delimited files}{p_end}
{phang2}{help pqsave##limits:Limitations}{p_end}
{phang2}{help pqsave##install:Installation}{p_end}

{marker how}{...}
{title:How pqsave reads your data}

{pstd}
When saving the data in memory, {cmd:pqsave} reads the original .dta file
directly, without writing anything, if all of the following hold: no {it:if} or
{it:in} was specified, the data are unchanged since they were loaded
({cmd:c(changed)} is 0), and the file on disk has the same number of
observations and variables as the data in memory. A {it:varlist} does not
prevent this.

{pstd}
Otherwise {cmd:pqsave} first {helpb save}s a temporary copy to {opt tmpdir()}.
Afterward it restores the dataset's file name and changed status, so a later
{cmd:save, replace} or {cmd:clear} behaves as it would have without
{cmd:pqsave}.

{pstd}
With {cmd:using}, a .dta file in Stata 13 format or newer (.dta formats
117{c -}119) is read directly. Older files are first converted by loading them
into a temporary frame and saving a copy.

{marker types}{...}
{title:Storage types}

{pstd}
Each Stata variable becomes a Parquet column of the corresponding type:

{p2colset 9 32 34 2}{...}
{p2col:Stata}Parquet (Arrow) type{p_end}
{p2line}
{p2col:{cmd:byte}}{cmd:int8}{p_end}
{p2col:{cmd:int}}{cmd:int16}{p_end}
{p2col:{cmd:long}}{cmd:int32}{p_end}
{p2col:{cmd:float}}{cmd:float32}{p_end}
{p2col:{cmd:double}}{cmd:float64}{p_end}
{p2col:{cmd:str}{it:#}, {cmd:strL}}{cmd:string} (UTF-8){p_end}
{p2col:{cmd:%td} format}{cmd:date32}{p_end}
{p2col:{cmd:%tc} format}{cmd:timestamp[ms]}{p_end}
{p2line}
{p2colreset}{...}

{pstd}
Missing values become Parquet nulls. Value-labeled variables are stored as
their numeric codes; the labels are kept in the metadata. Empty strings are
stored as empty strings, not nulls.

{marker csv}{...}
{title:Delimited files}

{pstd}
If the Python package {cmd:duckdb} is installed, it is used to convert
delimited files, with column types detected from the whole file. Otherwise
{cmd:pyarrow} streams the file, inferring types from the first 64 MB. If a later
row contradicts an inferred type (for example, text in a column that looked
numeric), that column is widened to {cmd:double} or {cmd:string} and the
conversion restarts; a note is displayed when this happens.

{pstd}
The following are read as missing in numeric columns: an empty field,
{cmd:.}, {cmd:.a}{c -}{cmd:.z}, {cmd:NA}, {cmd:N/A}, {cmd:NULL}, {cmd:NaN}, and
{cmd:#N/A}. No Stata metadata are stored for delimited input. Column names are
kept as they appear in the header, even if they are not valid Stata names.

{marker limits}{...}
{title:Limitations}

{phang2}
o Extended missing values ({cmd:.a}{c -}{cmd:.z}) are saved as ordinary nulls
and come back as {cmd:.}.{p_end}

{phang2}
o {cmd:%td} values stored as {cmd:float} or {cmd:double} are rounded down to
whole days. {cmd:%tc} values are rounded to whole milliseconds.{p_end}

{phang2}
o {cmd:%tC}, {cmd:%tw}, {cmd:%tm}, {cmd:%tq}, {cmd:%th}, and {cmd:%ty} variables
are saved as numbers. Their formats are restored by {cmd:pquse}.{p_end}

{phang2}
o {cmd:strL} variables are converted more slowly than other types.{p_end}

{phang2}
o Specifying {it:if} or {it:in} always requires a temporary copy of the full
dataset; rows are filtered while converting.{p_end}

{marker install}{...}
{title:Installation}

{pstd}
Place {cmd:pqsave.ado}, {cmd:pquse.ado}, {cmd:pqtools.py}, and their help files
in the same directory on the {help adopath:ado-path}. {cmd:pqtools.py} contains
the Python code for both commands. Check the Python setup with

{phang2}{cmd:. python query}{p_end}
{phang2}{cmd:. python: import pyarrow; print(pyarrow.__version__)}{p_end}

{pstd}
If you change {cmd:pqtools.py} during a Stata session, also change its version
number and the matching version in both .ado files, or restart Stata, so that
the new code is loaded.


{marker examples}{...}
{title:Examples}

{pstd}Save the data in memory to {cmd:auto.parquet} in the current directory{p_end}
{phang2}{cmd:. sysuse auto}{p_end}
{phang2}{cmd:. pqsave}{p_end}

{pstd}Choose the name and directory, and overwrite an existing file{p_end}
{phang2}{cmd:. pqsave, name(auto_2026) directory("D:/data/out") replace}{p_end}

{pstd}Save a subset of variables and observations{p_end}
{phang2}{cmd:. pqsave make price mpg if foreign, name(foreign_cars)}{p_end}

{pstd}Convert a .dta on disk without loading it{p_end}
{phang2}{cmd:. pqsave using "D:/data/panel_2024.dta", directory("D:/data/parquet")}{p_end}

{pstd}Convert a large CSV file{p_end}
{phang2}{cmd:. pqsave using "D:/raw/extract.csv", name(extract) replace verbose}{p_end}

{pstd}Faster compression, temp file on a local scratch disk{p_end}
{phang2}{cmd:. pqsave, compression(lz4) tmpdir("E:/scratch")}{p_end}


{marker results}{...}
{title:Stored results}

{pstd}
{cmd:pqsave} stores the following in {cmd:r()}:

{synoptset 20 tabbed}{...}
{p2col 5 20 24 2: Scalars}{p_end}
{synopt:{cmd:r(N)}}number of rows written{p_end}
{synopt:{cmd:r(k)}}number of columns written{p_end}
{synopt:{cmd:r(bytes)}}size of the Parquet file in bytes{p_end}

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
Help: {helpb pquse}, {helpb save}, {helpb python}
{p_end}
