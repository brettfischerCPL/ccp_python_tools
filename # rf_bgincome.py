# rf_bgincome.py  v15
# Random Forest prediction of census block group income.
# Executed from Stata via: python script "path/to/rf_bgincome.py"
#
# Hyperparameter logic:
#   - rf_hp_manual = 1: use specified maxfeatures/minleaf, ignore tune
#   - rf_do_tune = 1 and rf_hp_manual = 0: tune via RandomizedSearchCV
#   - neither: use defaults
#
# ntrees is always user-specifiable and defaults to 500.
#
# Required packages: pandas, numpy, scikit-learn, joblib, pyarrow, pyreadstat

import os
os.environ["OMP_NUM_THREADS"]        = "1"
os.environ["OPENBLAS_NUM_THREADS"]   = "1"
os.environ["MKL_NUM_THREADS"]        = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"]    = "1"

import warnings
warnings.filterwarnings("ignore")

import gc
import sys
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pyreadstat
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import RandomizedSearchCV, KFold
import joblib
import sfi

# ==============================================================================
# A. Retrieve parameters from Stata globals
# ==============================================================================
def _g(name):
    return sfi.Macro.getGlobal(name).strip().strip('"').strip("'")

yvar          = _g("rf_yvar")
predvar       = _g("rf_predvar")
predvar_level = _g("rf_predvar_level")
leavout       = int(_g("rf_leavout"))
leavout_var   = _g("rf_leavout_var")
do_log        = _g("rf_do_log")    == "1"
do_tune       = _g("rf_do_tune")   == "1"
hp_manual     = _g("rf_hp_manual") == "1"

# Manual hyperparameter values (-1 means not specified)
_mf = _g("rf_maxfeatures")
_ml = _g("rf_minleaf")
_nt = _g("rf_ntrees")
manual_maxfeatures = float(_mf)     if _mf not in ("", "-1") else None
manual_minleaf     = int(float(_ml)) if _ml not in ("", "-1") else None
n_estimators       = int(float(_nt)) if _nt not in ("", "-1") else 500

covariates    = [v for v in _g("rf_covariates").split()   if v]
covar_labels  = [v for v in _g("rf_covar_labels").split() if v]
cat_vars      = set(v for v in _g("rf_cat_vars").split()  if v)
idvars        = [v for v in _g("rf_idvars").split()       if v]
usecols       = [v for v in _g("rf_usecols").split()      if v] or None
touse_var     = _g("rf_touse")

extract_path  = _g("rf_extract_path")
pred_path     = _g("rf_pred_path")

if len(covariates) != len(covar_labels):
    print("ERROR: covariates list ({}) and labels list ({}) differ in length.".format(
          len(covariates), len(covar_labels)))
    sys.exit(1)

label_map = dict(zip(covariates, covar_labels))

print("  Parameters received from Stata:")
print("    Outcome variable       : " + yvar)
print("    Predicted varname      : " + predvar)
print("    Leave-out pct          : " + str(leavout))
print("    Tune                   : " + str(do_tune))
print("    Log outcome            : " + str(do_log))
print("    HP manual              : " + str(hp_manual))
print("    n_estimators           : " + str(n_estimators))
if manual_maxfeatures is not None:
    print("    max_features (manual)  : " + str(manual_maxfeatures))
if manual_minleaf is not None:
    print("    min_samples_leaf (manual) : " + str(manual_minleaf))
print("    Covariates count       : " + str(len(covariates)))
print("    Categorical count      : " + str(len(cat_vars)))
print("    ID variables           : " + " ".join(idvars))
print("    Usecols count          : " + str(len(usecols) if usecols else 0))
print("    Extract path           : " + extract_path)
print("    Predictions path       : " + pred_path)

if not covariates:
    print("ERROR: No covariates received from Stata.")
    sys.exit(1)

# ==============================================================================
# Helper: impute NaNs in a float32 array using column medians.
# Columns that are entirely NaN fall back to 0.
# ==============================================================================
def impute_array(arr, label="array"):
    n_missing = int(np.sum(np.isnan(arr)))
    if n_missing == 0:
        return arr
    print("WARNING: {} missing cells in {}. "
          "Applying column median imputation.".format(n_missing, label))
    col_medians = np.nanmedian(arr, axis=0).astype(np.float32)
    col_medians = np.where(np.isnan(col_medians), np.float32(0), col_medians)
    nan_rows, nan_cols = np.where(np.isnan(arr))
    arr[nan_rows, nan_cols] = col_medians[nan_cols]
    remaining = int(np.sum(np.isnan(arr)))
    print("  Imputation complete. Missing cells remaining: {}".format(remaining))
    return arr

# ==============================================================================
# Helper: build feature matrix from a DataFrame.
# Applies numeric coercion, extracts as float32, runs NaN imputation.
# df_in is deleted inside this function.
# ==============================================================================
def build_feature_matrix(df_in, feature_cols, label="matrix"):
    for c in feature_cols:
        df_in[c] = pd.to_numeric(df_in[c], errors="coerce")
    feature_data = df_in[feature_cols]
    del df_in
    gc.collect()
    X = feature_data.values.astype(np.float32)
    del feature_data
    gc.collect()
    X = impute_array(X, label=label)
    return X

# ==============================================================================
# Helper: read the .dta extract, filter to touse rows, and expand
# categorical variables to dummies. Returns the processed DataFrame.
# ==============================================================================
def read_and_expand(extract_path, usecols, touse_var,
                    categorical_vars, label_map):
    df_out, _ = pyreadstat.read_dta(
        extract_path,
        apply_value_formats = False,
        usecols             = usecols,
    )
    if touse_var and touse_var in df_out.columns:
        df_out = df_out[df_out[touse_var] == 1].reset_index(drop=True)
    for v in categorical_vars:
        col      = df_out[v].astype(str).replace("nan", np.nan)
        readable = label_map[v]
        dummies  = pd.get_dummies(col, prefix=readable, drop_first=False,
                                   dummy_na=False)
        if dummies.shape[1] > 1:
            dummies = dummies.iloc[:, 1:]
        df_out = df_out.drop(columns=[v])
        df_out = pd.concat([df_out, dummies], axis=1)
        del dummies, col
        gc.collect()
    return df_out

# ==============================================================================
# B. Read needed columns from the full .dta via pyreadstat usecols
# ==============================================================================
print("  Reading extract columns from .dta ...")

df, meta = pyreadstat.read_dta(
    extract_path,
    apply_value_formats = False,
    usecols             = usecols,
)

print("  Rows read : " + str(len(df)))
print("  Cols read : " + str(len(df.columns)))

missing_cols = [v for v in covariates + [yvar] if v not in df.columns]
if missing_cols:
    print("ERROR: Column(s) missing from extract: " + ", ".join(missing_cols))
    sys.exit(1)
for v in idvars:
    if v not in df.columns:
        print("ERROR: ID variable '{}' missing from extract.".format(v))
        sys.exit(1)

# Filter to touse rows
if touse_var and touse_var in df.columns:
    df = df[df[touse_var] == 1].reset_index(drop=True)
    print("  Rows after touse filter : " + str(len(df)))

nobs  = len(df)
y_arr = df[yvar].values.astype(float)
print("  Obs with non-missing {}: {}".format(
      yvar, int(np.sum(~np.isnan(y_arr)))))

# Extract idvar columns and leavout column before any deletions
id_data = df[idvars].copy()

leavout_arr = (
    (df[leavout_var].values == 1)
    if (leavout_var and leavout_var in df.columns)
    else np.zeros(nobs, dtype=bool)
)

if leavout_var and leavout_var not in df.columns:
    print("WARNING: leavout variable '{}' not found. No holdout applied.".format(
          leavout_var))

# ==============================================================================
# C. One-hot encode categorical variables and build feature matrix
# ==============================================================================
continuous_vars  = [v for v in covariates if v not in cat_vars]
categorical_vars = [v for v in covariates if v in cat_vars]

for v in continuous_vars:
    df[v] = pd.to_numeric(df[v], errors="coerce")

feature_cols   = []
feature_labels = []

for v in continuous_vars:
    feature_cols.append(v)
    feature_labels.append(label_map[v])

for v in categorical_vars:
    col      = df[v].astype(str).replace("nan", np.nan)
    readable = label_map[v]
    dummies  = pd.get_dummies(col, prefix=readable, drop_first=False,
                               dummy_na=False)
    if dummies.shape[1] > 1:
        dummies = dummies.iloc[:, 1:]
    for col_name in dummies.columns.tolist():
        feature_cols.append(col_name)
        feature_labels.append(col_name)
    df = df.drop(columns=[v])
    df = pd.concat([df, dummies], axis=1)
    del dummies, col
    gc.collect()

print("  Feature matrix : {} columns after dummy expansion.".format(
      len(feature_cols)))

X_all = build_feature_matrix(df, feature_cols, label="X_all")
print("  Feature matrix RAM : {:.1f} GB (float32)".format(
      X_all.nbytes / 1e9))

# ==============================================================================
# D. Define training / holdout masks and extract training data.
#    X_all deleted immediately after X_train extracted.
# ==============================================================================
mask_y_obs = ~np.isnan(y_arr)
mask_ho    = leavout_arr
mask_train = mask_y_obs & ~mask_ho

n_train = int(np.sum(mask_train))
print("  Training observations : " + str(n_train))

if n_train < 10:
    print("ERROR: Fewer than 10 training observations ({}).".format(n_train))
    sys.exit(1)

X_train = X_all[mask_train].copy()
y_train = y_arr[mask_train].copy()

X_train = impute_array(X_train, label="X_train")

del X_all
gc.collect()

# ==============================================================================
# E. Determine hyperparameters
# ==============================================================================
n_features = X_train.shape[1]
sqrt_frac  = round(np.sqrt(n_features) / n_features, 4)

if hp_manual:
    best_params = {
        "max_features"    : manual_maxfeatures if manual_maxfeatures is not None
                            else sqrt_frac,
        "min_samples_leaf": manual_minleaf if manual_minleaf is not None
                            else 5,
    }
    print("  Hyperparameter mode       : manual")
    print("  max_features              : " + str(best_params["max_features"]))
    print("  min_samples_leaf          : " + str(best_params["min_samples_leaf"]))
    if do_tune:
        print("  (tune option ignored — manual hyperparameters take precedence)")

elif do_tune:
    print("  Tuning hyperparameters via RandomizedSearchCV ...")

    max_leaf = max(6, int(n_train * 0.01))
    min_samples_leaf_grid = sorted(set(
        int(x) for x in
        np.unique(np.logspace(np.log10(5), np.log10(max_leaf), num=20).astype(int))
    ))
    max_features_grid = sorted(set([sqrt_frac, 0.2, 0.3, 0.5]))

    param_dist = {
        "min_samples_leaf": min_samples_leaf_grid,
        "max_features"    : max_features_grid,
    }

    print("  min_samples_leaf candidates: " + str(min_samples_leaf_grid))
    print("  max_features candidates    : " + str(max_features_grid))

    base_rf = RandomForestRegressor(
        n_estimators=100,
        random_state=42,
        n_jobs=1,
        oob_score=False,
    )

    cv_splitter = KFold(n_splits=5, shuffle=True, random_state=42)

    search = RandomizedSearchCV(
        estimator           = base_rf,
        param_distributions = param_dist,
        n_iter              = 20,
        scoring             = "r2",
        cv                  = cv_splitter,
        random_state        = 42,
        n_jobs              = -1,
        verbose             = 1,
        refit               = False,
    )

    with joblib.parallel_backend("threading"):
        search.fit(X_train, y_train)

    best_params = search.best_params_
    best_cv_r2  = search.best_score_

    print("  Best min_samples_leaf : " + str(best_params["min_samples_leaf"]))
    print("  Best max_features     : " + str(best_params["max_features"]))
    print("  Best CV R2            : {:.4f}".format(best_cv_r2))

else:
    best_params = {
        "min_samples_leaf": 5,
        "max_features"    : sqrt_frac,
    }
    print("  Hyperparameter mode       : default")
    print("  min_samples_leaf          : " + str(best_params["min_samples_leaf"]))
    print("  max_features              : " + str(best_params["max_features"]))

# ==============================================================================
# F. Fit final Random Forest
# ==============================================================================
print("  Fitting final Random Forest ...")
print("  n_estimators          : " + str(n_estimators))
print("  min_samples_leaf      : " + str(best_params["min_samples_leaf"]))
print("  max_features          : " + str(best_params["max_features"]))
print("  Training outcome summary:")
print("    N    : {}".format(len(y_train)))
print("    Mean : {:.4f}".format(np.mean(y_train)))
print("    SD   : {:.4f}".format(np.std(y_train)))
print("    Min  : {:.4f}".format(np.min(y_train)))
print("    Max  : {:.4f}".format(np.max(y_train)))

with joblib.parallel_backend("threading"):
    rf = RandomForestRegressor(
        n_estimators     = n_estimators,
        min_samples_leaf = best_params["min_samples_leaf"],
        max_features     = best_params["max_features"],
        max_depth        = None,
        n_jobs           = -1,
        random_state     = 42,
        oob_score        = True,
    )
    rf.fit(X_train, y_train)

print("  Out-of-bag R2 : {:.4f}".format(rf.oob_score_))

# Holdout R2
if leavout > 0 and mask_ho.any():
    valid_ho = mask_ho & mask_y_obs
    if valid_ho.sum() > 0:
        print("  Computing holdout R2 ...")
        df_ho  = read_and_expand(extract_path, usecols, touse_var,
                                 categorical_vars, label_map)
        X_ho   = build_feature_matrix(df_ho, feature_cols, label="X_ho")
        y_ho_true = y_arr[valid_ho]
        y_ho_pred = rf.predict(X_ho[mask_ho])
        del X_ho
        gc.collect()
        ss_res = np.sum((y_ho_true - y_ho_pred) ** 2)
        ss_tot = np.sum((y_ho_true - np.mean(y_ho_true)) ** 2)
        ho_r2  = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
        print("  Holdout R2    : {:.4f}".format(ho_r2))

importances = pd.Series(rf.feature_importances_, index=feature_labels)
top10 = importances.nlargest(10)
print("  Top-10 feature importances:")
for fname, imp in top10.items():
    print("    {:<40s}  {:.4f}".format(fname, imp))

del X_train, y_train
gc.collect()

# ==============================================================================
# G. Reconstruct full feature matrix from disk and generate predictions
# ==============================================================================
print("  Reconstructing feature matrix for prediction ...")

df_pred    = read_and_expand(extract_path, usecols, touse_var,
                             categorical_vars, label_map)
X_all_pred = build_feature_matrix(df_pred, feature_cols, label="X_all_pred")

print("  Generating predictions ...")
y_pred_all = rf.predict(X_all_pred)

if do_log:
    print("  Computing Jensen correction ...")
    df_log      = read_and_expand(extract_path, usecols, touse_var,
                                  categorical_vars, label_map)
    y_log       = df_log[yvar].values.astype(float)
    mask_tr_log = ~np.isnan(y_log) & ~leavout_arr
    X_log       = build_feature_matrix(df_log, feature_cols, label="X_log")
    resid_var   = np.var(
        y_log[mask_tr_log] - rf.predict(X_log[mask_tr_log]), ddof=1
    )
    y_pred_all_level = np.exp(y_pred_all + 0.5 * resid_var)
    del X_log, y_log
    gc.collect()

del X_all_pred
gc.collect()

print("  Predicted outcome summary (all touse rows):")
print("    Mean : {:.4f}".format(np.mean(y_pred_all)))
print("    SD   : {:.4f}".format(np.std(y_pred_all)))
print("    Min  : {:.4f}".format(np.min(y_pred_all)))
print("    Max  : {:.4f}".format(np.max(y_pred_all)))

# ==============================================================================
# H. Write predictions to a small Parquet file keyed on idvars
# ==============================================================================
print("  Writing predictions Parquet ...")

pred_df = id_data.copy()
del id_data
gc.collect()

pred_df[predvar] = y_pred_all
if do_log:
    pred_df[predvar_level] = y_pred_all_level

pq.write_table(
    pa.Table.from_pandas(pred_df, preserve_index=False),
    pred_path,
    compression="snappy",
)

print("  Predictions written : {} rows x {} cols -> {}".format(
      len(pred_df), len(pred_df.columns), pred_path))
print("  Done.")