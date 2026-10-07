{smcl}
{* *! version 1.0.0  07oct2026}{...}
{vieweralsosee "pqsave" "help pqsave"}{...}
{vieweralsosee "pquse" "help pquse"}{...}
{vieweralsosee "[P] PyStata integration" "help python"}{...}
{title:Title}

{phang}
{bf:pqsetup} {hline 2} Check and install the Python packages used by pqsave and pquse


{title:Syntax}

{p 8 17 2}
{cmd:pqsetup}
[{cmd:,} {opt wheel:house(path)} {opt noinstall} {opt force}]


{title:Description}

{pstd}
{helpb pqsave} and {helpb pquse} run {cmd:pqsetup} automatically. It checks that
the Python used by Stata has {cmd:numpy} 1.21 or newer and {cmd:pyarrow} 12.0 or
newer. If either is missing or too old, {cmd:pqsetup} installs it with

{phang2}{cmd:"}{it:python}{cmd:" -m pip install --no-index --find-links="}{it:wheelhouse}{cmd:" --upgrade} {it:packages}{p_end}

{pstd}
which installs from a folder of package files instead of the internet. After a
successful check, later calls in the same Stata session skip it.

{pstd}
Typed by itself, {cmd:pqsetup} displays the Python executable and the package
versions in use.


{title:Options}

{phang}
{opt wheelhouse(path)} specifies the folder of Python package files ({cmd:.whl})
to install from. The default is the global macro {cmd:PQ_WHEELHOUSE} if it is
set, and {cmd:S:/python/packages} otherwise. To change the default for every
command, add {cmd:global PQ_WHEELHOUSE "}{it:path}{cmd:"} to your
{help profile:profile.do}.

{phang}
{opt noinstall} reports missing or outdated packages and stops with an error
instead of installing them.

{phang}
{opt force} repeats the check even if it already succeeded in this session.


{title:Remarks}

{pstd}
If {cmd:numpy} or {cmd:pyarrow} was already loaded in this Stata session before
being upgraded, the new version cannot take effect until Stata is restarted.
{cmd:pqsetup} says so when this happens.

{pstd}
The package folder must contain files built for the same Python version and
operating system as the Python Stata uses, including any packages that
{cmd:pyarrow} and {cmd:numpy} depend on.

{pstd}
If installation fails with a permissions error, the Python installation is
probably shared. {cmd:pqsetup} displays the full {cmd:pip} command; add
{cmd:--user} to it and run it with {helpb shell}, then restart Stata.


{title:Examples}

{phang2}{cmd:. pqsetup}{p_end}
{phang2}{cmd:. pqsetup, force}{p_end}
{phang2}{cmd:. pqsetup, wheelhouse("S:/python/packages_py311")}{p_end}


{title:Author}

{pstd}
California Policy Lab. Source code is maintained in CPL's OneDev repository on
the secure server.{p_end}


{title:Also see}

{psee}
Help: {helpb pqsave}, {helpb pquse}, {helpb python}
{p_end}
