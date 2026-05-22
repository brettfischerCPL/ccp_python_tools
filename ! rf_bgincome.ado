*! rf_bgincome.ado  v15
*! Random Forest prediction of census block group income from individual-level data.
*!
*! Syntax:
*!   rf_bgincome varname [if] [in], driver(filepath) pydir(dirpath) idvars(varlist)
*!                                  [sheet(sheetname)] [leavout(#)] [tune]
*!                                  [logoutcome] [maxfeatures(#)] [minleaf(#)]
*!                                  [ntrees(#)]
*!
*! Hyperparameter logic:
*!   - maxfeatures() and/or minleaf() specified : use those values directly
*!   - tune specified, no manual hyperparameters : tune via RandomizedSearchCV
*!   - neither specified                         : use defaults
*!
*!   - ntrees() always overrides the default of 500 trees when specified.
*!
*! All dummy expansion and imputation for categorical variables is done in Stata.
*! Python receives only numeric, fully-imputed columns.
*!
*! Memory optimisation:
*!   - compress downcasts all variables to smallest storage type before save
*!   - Full dataset saved to Parquet, then Stata memory is cleared entirely
*!     so Python has the machine's full RAM available during rf.fit
*!   - After Python exits, Stata reloads from the same Parquet file
*!
*! gtools is used throughout for speed.
*!
*! Requirements:
*!   - Stata 16+ with Python integration enabled
*!   - Stata user-written commands: gtools, pq
*!   - Python packages: numpy, scikit-learn, joblib, pyarrow, pandas (minimal)
*!   - rf_bgincome.py must exist in the directory specified by pydir()
*!   - rf_bgincome_tune.py must exist in pydir() if tune option is used

capture program drop rf_bgincome
program define rf_bgincome
    version 16

    // -------------------------------------------------------------------------
    // 1. PARSE SYNTAX
    // -------------------------------------------------------------------------
    syntax varname [if] [in],       ///
        DRIVer(string)              ///
        PYDIr(string)               ///
        IDVars(varlist)             ///
        [SHEET(string)]             ///
        [LEAVout(integer 0)]        ///
        [TUNE]                      ///
        [LOGoutcome]                ///
        [MAXFeatures(real -1)]      ///
        [MINLeaf(integer -1)]       ///
        [NTrees(integer -1)]

    if `leavout' != 0 & (`leavout' < 1 | `leavout' > 99) {
        di as error "leavout() must be an integer between 1 and 99."
        exit 198
    }

    if `maxfeatures' != -1 {
        if `maxfeatures' <= 0 | `maxfeatures' > 1 {
            di as error "maxfeatures() must be a number between 0 and 1 (exclusive)."
            exit 198
        }
    }

    if `minleaf' != -1 {
        if `minleaf' < 1 {
            di as error "minleaf() must be a positive integer."
            exit 198
        }
    }

    if `ntrees' != -1 {
        if `ntrees' < 1 {
            di as error "ntrees() must be a positive integer."
            exit 198
        }
        if `ntrees' < 50 {
            di as text "Warning: ntrees(`ntrees') is very small. " ///
                       "Predictions may be unstable."
        }
    }

    if (`maxfeatures' != -1 & `minleaf' == -1) | ///
       (`maxfeatures' == -1 & `minleaf' != -1) {
        di as text "Note: only one of maxfeatures()/minleaf() specified. " ///
                   "The other will use its default value."
    }

    local yvar    `varlist'
    local do_tune = ("`tune'" == "tune")
    local do_log  = ("`logoutcome'" == "logoutcome")
    local hp_manual = (`maxfeatures' != -1 | `minleaf' != -1)

    if `hp_manual' {
        di as result "  Hyperparameter mode        : manual"
        if `maxfeatures' != -1 {
            di as result "  max_features               : `maxfeatures'"
        }
        else {
            di as result "  max_features               : (default)"
        }
        if `minleaf' != -1 {
            di as result "  min_samples_leaf           : `minleaf'"
        }
        else {
            di as result "  min_samples_leaf           : (default)"
        }
        if `do_tune' {
            di as text "Note: tune option ignored because hyperparameters " ///
                       "are specified manually."
        }
    }
    else if `do_tune' {
        di as result "  Hyperparameter mode        : tune (RandomizedSearchCV)"
    }
    else {
        di as result "  Hyperparameter mode        : default"
    }

    if `ntrees' != -1 {
        di as result "  Number of trees            : `ntrees'"
    }
    else {
        di as result "  Number of trees            : 500 (default)"
    }

    local predvar "rf_pred_`yvar'"
    if length("`predvar'") > 32 {
        local predvar = substr("`predvar'", 1, 32)
    }
    local predvar_level = substr("`predvar'", 1, 26) + "_level"

    // -------------------------------------------------------------------------
    // 2. RESOLVE PYTHON SCRIPT PATH
    // -------------------------------------------------------------------------
    local pydir = rtrim("`pydir'")
    if substr("`pydir'", length("`pydir'"), 1) == "/" | ///
       substr("`pydir'", length("`pydir'"), 1) == "\" {
        local pydir = substr("`pydir'", 1, length("`pydir'") - 1)
    }

    local pypath_main "`pydir'/rf_bgincome.py"
    capture confirm file "`pypath_main'"
    if _rc != 0 {
        di as error "Python script not found: `pypath_main'"
        exit 601
    }

    // -------------------------------------------------------------------------
    // 3. VALIDATE idvars() UNIQUELY IDENTIFY OBSERVATIONS
    // -------------------------------------------------------------------------
    di as result "  Checking idvars uniqueness ..."
    tempvar dup_flag
    quietly gduplicates tag `idvars', generate(`dup_flag')
    quietly count if `dup_flag' > 0
    if r(N) > 0 {
        di as error "idvars(`idvars') do not uniquely identify observations."
        di as error "`r(N)' observations share an idvars combination with at least one other."
        drop `dup_flag'
        exit 459
    }
    drop `dup_flag'
    di as result "  idvars uniqueness check    : PASSED"

    // -------------------------------------------------------------------------
    // 4. READ DRIVER FILE INTO A STATA FRAME
    // -------------------------------------------------------------------------
    capture frame drop _rf_driver_frame
    frame create _rf_driver_frame
    frame _rf_driver_frame {
        qui import excel using "`driver'",  ///
            `=cond("`sheet'"!="","sheet(`sheet')","")'  ///
            firstrow case(lower) clear

        local var_col ""
        foreach candidate in variable var variables vars varname varnames {
            capture confirm variable `candidate'
            if _rc == 0 {
                local var_col "`candidate'"
                continue, break
            }
        }
        if "`var_col'" == "" {
            di as error "Driver file must contain a column named 'variable', 'var', or similar."
            exit 111
        }

        local cat_col ""
        foreach candidate in categorical cat is_cat iscategorical ///
                             dummy factor categorical_flag {
            capture confirm variable `candidate'
            if _rc == 0 {
                local cat_col "`candidate'"
                continue, break
            }
        }

        local covariates ""
        local cat_vars   ""
        local n_driver = _N
        forvalues r = 1/`n_driver' {
            local vname = strtrim(`var_col'[`r'])
            if "`vname'" != "" & "`vname'" != "." {
                local covariates "`covariates' `vname'"
                if "`cat_col'" != "" {
                    local cflag = `cat_col'[`r']
                    if !missing(`cflag') & `cflag' == 1 {
                        local cat_vars "`cat_vars' `vname'"
                    }
                }
            }
        }
        local covariates = strtrim("`covariates'")
        local cat_vars   = strtrim("`cat_vars'")
    }
    frame drop _rf_driver_frame

    di as result "  Covariates from driver     : `=wordcount("`covariates'")'"
    di as result "  Categorical variables       : `cat_vars'"

    // -------------------------------------------------------------------------
    // 5. VALIDATE COVARIATES EXIST IN MAIN DATASET
    // -------------------------------------------------------------------------
    foreach v of local covariates {
        capture confirm variable `v'
        if _rc != 0 {
            di as error "Variable '`v'' listed in driver not found in dataset."
            exit 111
        }
    }

    // -------------------------------------------------------------------------
    // 6. MARK TOUSE SAMPLE
    // -------------------------------------------------------------------------
    tempvar touse
    quietly gen byte `touse' = 0
    quietly replace  `touse' = 1 `if' `in'

    quietly count if `touse'
    local n_touse = r(N)
    quietly count if `touse' & !missing(`yvar')
    local n_train_elig = r(N)
    quietly count if `touse' & missing(`yvar')
    local n_pred_only = r(N)

    di as result "  Touse observations         : `n_touse'"
    di as result "  Training-eligible (y obs.) : `n_train_elig'"
    di as result "  Predict-only (y missing)   : `n_pred_only'"

    if `n_train_elig' < 10 {
        di as error "Fewer than 10 training observations. Aborting."
        exit 2001
    }

    // -------------------------------------------------------------------------
    // 7. IMPUTE MISSING VALUES, CREATE MISSING-FLAG DUMMIES, AND
    //    EXPAND CATEGORICALS TO INDICATOR DUMMIES (ALL IN STATA)
    //
    //    After this step, rf_features contains ONLY numeric, fully-imputed
    //    columns: continuous variables, imputed tempvars, missing-flag
    //    dummies, and expanded categorical indicator dummies.
    // -------------------------------------------------------------------------
    local rf_features  ""

    foreach v of local covariates {

        local is_cat 0
        foreach cv of local cat_vars {
            if "`cv'" == "`v'" local is_cat 1
        }

        quietly count if `touse' & missing(`v')
        local n_miss = r(N)

        if `n_miss' == 0 & `is_cat' == 0 {
            // Continuous, no missings: use directly
            local rf_features "`rf_features' `v'"
        }
        else if `n_miss' == 0 & `is_cat' == 1 {
            // Categorical, no missings: expand to dummies
            quietly glevelsof `v' if `touse', local(cat_levels)
            local first_level = 1
            foreach lev of local cat_levels {
                if `first_level' {
                    local first_level = 0
                }
                else {
                    tempvar d_`v'_`lev'
                    quietly gen byte `d_`v'_`lev'' = ///
                        (`v' == `lev') if `touse'
                    quietly replace `d_`v'_`lev'' = 0 ///
                        if `touse' & missing(`d_`v'_`lev'')
                    local rf_features "`rf_features' `d_`v'_`lev''"
                }
            }
        }
        else if `is_cat' == 0 {
            // Continuous with missings: impute to median + missing flag
            tempvar imp_v
            quietly gen double `imp_v' = `v' if `touse'

            tempvar med_v
            quietly gegen double `med_v' = pctile(`v') ///
                if `touse' & !missing(`yvar'), p(50)
            quietly replace `imp_v' = `med_v' if `touse' & missing(`imp_v')
            drop `med_v'

            local rf_features "`rf_features' `imp_v'"

            tempvar mflag_v
            quietly gen byte `mflag_v' = (`touse' & missing(`v'))
            local rf_features "`rf_features' `mflag_v'"
        }
        else {
            // Categorical with missings: impute to mode, then expand
            tempvar imp_v
            quietly gen double `imp_v' = `v' if `touse'

            tempvar freq_tmp maxfreq_tmp
            quietly gegen long `freq_tmp' = count(`v') ///
                if `touse' & !missing(`v') & !missing(`yvar'), by(`v')
            quietly gegen long `maxfreq_tmp' = max(`freq_tmp')
            quietly glevelsof `v' ///
                if `freq_tmp' == `maxfreq_tmp' & !missing(`v'), ///
                local(mode_vals)
            quietly replace `imp_v' = `=word("`mode_vals'", 1)' ///
                if `touse' & missing(`imp_v')

            // Missing flag
            tempvar mflag_v
            quietly gen byte `mflag_v' = (`touse' & missing(`v'))
            local rf_features "`rf_features' `mflag_v'"

            // Expand imputed categorical to dummies
            quietly glevelsof `imp_v' if `touse', local(cat_levels)
            local first_level = 1
            foreach lev of local cat_levels {
                if `first_level' {
                    local first_level = 0
                }
                else {
                    tempvar d_`v'_`lev'
                    quietly gen byte `d_`v'_`lev'' = ///
                        (`imp_v' == `lev') if `touse'
                    quietly replace `d_`v'_`lev'' = 0 ///
                        if `touse' & missing(`d_`v'_`lev'')
                    local rf_features "`rf_features' `d_`v'_`lev''"
                }
            }
        }
    }

    local rf_features  = strtrim("`rf_features'")
    local n_feat_total = wordcount("`rf_features'")

    di as result "  Total features (after expansion) : `n_feat_total'"

    // -------------------------------------------------------------------------
    // 8. HOLDOUT SAMPLE ASSIGNMENT
    // -------------------------------------------------------------------------
    if `leavout' != 0 {
        capture drop sample_leavout
        quietly gen byte sample_leavout = 0

        local n_holdout = max(1, round(`n_train_elig' * `leavout' / 100))
        di as result "  Holdout obs                : `n_holdout' (`leavout'% of training-eligible)"

        tempvar orig_order rand_order seq_tmp
        quietly gen long   `orig_order' = _n
        set seed 42
        quietly gen double `rand_order' = runiform() if `touse' & !missing(`yvar')
        quietly hashsort `rand_order'
        quietly gen long  `seq_tmp'     = _n if `touse' & !missing(`yvar')
        quietly replace sample_leavout  = 1  if `seq_tmp' <= `n_holdout'
        quietly hashsort `orig_order'
    }

    // -------------------------------------------------------------------------
    // 9. BUILD FILE PATHS (using absolute paths for reliability)
    // -------------------------------------------------------------------------
    local driver_dir = substr("`driver'", 1, strrpos("`driver'", "/"))
    if "`driver_dir'" == "" {
        local driver_dir = substr("`driver'", 1, strrpos("`driver'", "\"))
    }
    if "`driver_dir'" == "" {
        local driver_dir "`c(pwd)'/"
    }

    local extract_path "`driver_dir'_rf_extract_tmp.parquet"
    local pred_path    "`driver_dir'_rf_predictions_tmp.parquet"

    di as result "  Extract path               : `extract_path'"
    di as result "  Predictions path           : `pred_path'"

    // -------------------------------------------------------------------------
    // 10. COMPRESS, SAVE TO PARQUET, CLEAR STATA MEMORY
    //
    //     compress downcasts all variables to smallest storage type.
    //     pq save writes the full dataset to Parquet.
    //     clear frees Stata's entire dataset from RAM so Python has the
    //     machine's full memory available during rf.fit.
    //     After Python exits, Stata reloads from the same Parquet file.
    // -------------------------------------------------------------------------
    di as result "  Compressing dataset ..."
    quietly compress

    di as result "  Saving dataset as Parquet ..."
    quietly pq save "`extract_path'", replace

    // Verify the file was created
    capture confirm file "`extract_path'"
    if _rc != 0 {
        di as error "ERROR: Parquet file not created at: `extract_path'"
        exit 601
    }
    di as result "  Parquet file confirmed     : `extract_path'"

    di as result "  Clearing Stata memory (freeing RAM for Python) ..."
    clear

    // -------------------------------------------------------------------------
    // 11. BUILD USECOLS LIST AND PASS PARAMETERS TO PYTHON
    // -------------------------------------------------------------------------
    local extract_vars "`idvars' `yvar' `rf_features'"
    if `leavout' != 0 local extract_vars "`extract_vars' sample_leavout"
    local extract_vars_d ""
    foreach v of local extract_vars {
        local already 0
        foreach w of local extract_vars_d {
            if "`w'" == "`v'" local already 1
        }
        if !`already' local extract_vars_d "`extract_vars_d' `v'"
    }

    global rf_yvar          "`yvar'"
    global rf_predvar       "`predvar'"
    global rf_predvar_level "`predvar_level'"
    global rf_leavout       "`leavout'"
    global rf_leavout_var   "`=cond(`leavout'!=0,"sample_leavout","")'"
    global rf_covariates    "`rf_features'"
    global rf_covar_labels  "`rf_features'"
    global rf_do_tune       "`do_tune'"
    global rf_do_log        "`do_log'"
    global rf_idvars        "`idvars'"
    global rf_usecols       "`extract_vars_d'"
    global rf_touse         "`touse'"
    global rf_extract_path  "`extract_path'"
    global rf_pred_path     "`pred_path'"
    global rf_hp_manual     "`hp_manual'"
    global rf_maxfeatures   "`maxfeatures'"
    global rf_minleaf       "`minleaf'"
    global rf_ntrees        "`ntrees'"

    di as result "  Running RF estimation ..."
    python script "`pypath_main'"

    macro drop rf_yvar rf_predvar rf_predvar_level rf_leavout rf_leavout_var ///
               rf_covariates rf_covar_labels rf_do_tune rf_do_log           ///
               rf_idvars rf_usecols rf_touse rf_extract_path rf_pred_path   ///
               rf_hp_manual rf_maxfeatures rf_minleaf rf_ntrees

    // -------------------------------------------------------------------------
    // 12. RELOAD DATASET FROM PARQUET AND MERGE PREDICTIONS
    // -------------------------------------------------------------------------
    di as result "  Reloading dataset from Parquet ..."
    pq use "`extract_path'", clear
    capture erase "`extract_path'"

    di as result "  Merging predictions ..."

    capture frame drop _rf_pred_frame
    frame create _rf_pred_frame
    frame _rf_pred_frame {
        pq use "`pred_path'", clear
    }

    frlink m:1 `idvars', frame(_rf_pred_frame)
    frget `predvar' = `predvar', from(_rf_pred_frame)
    if `do_log' {
        frget `predvar_level' = `predvar_level', from(_rf_pred_frame)
    }
    drop _rf_pred_frame

    frame drop _rf_pred_frame
    capture erase "`pred_path'"

    // -------------------------------------------------------------------------
    // 13. LABEL PREDICTION VARIABLES
    // -------------------------------------------------------------------------
    quietly label variable `predvar' "RF predicted: `yvar'"
    if `do_log' {
        quietly label variable `predvar_level' "RF predicted level: `yvar'"
    }

    // -------------------------------------------------------------------------
    // 14. REPORT
    // -------------------------------------------------------------------------
    di as result _n "Random Forest prediction complete."
    di as result "Predicted values stored in : " as input "`predvar'"
    if `do_log' {
        di as result "Level predictions stored in: " as input "`predvar_level'"
        di as result "(Jensen's inequality correction applied)"
    }
    if `leavout' != 0 {
        di as result "Leave-out share            : " as input "`leavout'%"
        di as result "Leave-out indicator stored : " as input "sample_leavout"
    }

end