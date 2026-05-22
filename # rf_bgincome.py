# rf_bgincome.py  v22
# Random Forest prediction of census block group income.
# Executed from Stata via: python script "path/to/rf_bgincome.py"
#
# All dummy expansion and imputation is done in Stata. Python receives
# only numeric, fully-imputed columns.
#
# Stata clears its memory before calling this script, so Python has the
# machine's full RAM available. The feature matrix is built column-by-column
# from a PyArrow Table into a pre-allocated float32 array.
#
# X_train is a zero-copy view of X_all achieved by reordering rows so
# training observations are contiguous at the top.
#
# Holdout R2 is computed from the full predictions array after predict,
# requiring zero additional RAM or rf.predict calls.
#
# Hyperparameter tuning (if requested) is handled by rf_bgincome_tune.py.
#
# Required packages: numpy, scikit-learn, joblib, pyarrow, pandas (minimal)

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
from sklearn.ensemble import RandomForestRegressor
import joblib
import sfi

# ==============================================================================
# RAM diagnostic utility (array-level only)
# ==============================================================================
def print_ram(step_label, new_objects=None):
    """Print RAM diagnostics for named objects."""
    print("  RAM [{:<30s}]".format(step_label))
    if new_objects:
        for name, obj in new_objects.items():
            if isinstance(obj, np.ndarray):
                print("    {} : {:.2f} GB (shape={}, dtype={})".format(
                      name, obj.nbytes / 1e9, obj.shape, obj.dtype))
            elif isinstance(obj, pd.DataFrame):
                mem = obj.memory_usage(deep=True).sum() / 1e9
                print("    {} : {:.2f} GB (shape={})".format(
                      name, mem, obj.shape))
            elif isinstance(obj, pa.Table):
                print("    {} : {:.2f} GB (rows={}, cols={})".format(
                      name, obj.nbytes / 1e9, obj.num_rows, obj.num_columns))
            else:
                print("    {} : (size unknown)".format(name))

# ==============================================================================
# Helper: assert no NaN values remain.
# ==============================================================================
def check_no_nans(arr, label="array"):
    n_missing = int(np.sum(np.isnan(arr)))
    if n_missing > 0:
        print("ERROR: {} NaN cells found in {}. "
              "Stata imputation may have failed.".format(n_missing, label))
        sys.exit(1)

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

_mf = _g("rf_maxfeatures")
_ml = _g("rf_minleaf")
_nt = _g("rf_ntrees")
manual_maxfeatures = float(_mf)      if _mf not in ("", "-1") else None
manual_minleaf     = int(float(_ml)) if _ml not in ("", "-1") else None
n_estimators       = int(float(_nt)) if _nt not in ("", "-1") else 500

covariates    = [v for v in _g("rf_covariates").split()   if v]
covar_labels  = [v for v in _g("rf_covar_labels").split() if v]
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
print("    ID variables           : " + " ".join(idvars))
print("    Extract path           : " + extract_path)
print("    Predictions path       : " + pred_path)

if not covariates:
    print("ERROR: No covariates received from Stata.")
    sys.exit(1)

# ==============================================================================
# B. Read the Parquet extract, filter to touse rows
# ==============================================================================
print("  Reading Parquet extract ...")
table = pq.read_table(extract_path, columns=usecols)
print("  Rows read (full dataset) : " + str(table.num_rows))
print_ram("after pq.read_table", {"table": table})

if touse_var and touse_var in table.column_names:
    touse_mask = table.column(touse_var).to_numpy(zero_copy_only=False).astype(bool)
    table = table.filter(pa.array(touse_mask))
    del touse_mask
    gc.collect()

nobs = table.num_rows
print("  Rows after touse filter  : " + str(nobs))
print_ram("after touse filter", {"table": table})

# Validate columns
for v in covariates + [yvar]:
    if v not in table.column_names:
        print("ERROR: Column '{}' missing from extract.".format(v))
        sys.exit(1)
for v in idvars:
    if v not in table.column_names:
        print("ERROR: ID variable '{}' missing from extract.".format(v))
        sys.exit(1)

# Extract y, idvars, and leavout
y_arr = table.column(yvar).to_numpy(zero_copy_only=False).astype(float)
print("  Obs with non-missing {}: {}".format(
      yvar, int(np.sum(~np.isnan(y_arr)))))

id_data = table.select(idvars).to_pandas()

leavout_arr = (
    table.column(leavout_var).to_numpy(zero_copy_only=False).astype(bool)
    if (leavout_var and leavout_var in table.column_names)
    else np.zeros(nobs, dtype=bool)
)

if leavout_var and leavout_var not in table.column_names:
    print("WARNING: leavout variable '{}' not found. No holdout applied.".format(
          leavout_var))

print_ram("after extracting y/id/leavout",
          {"y_arr": y_arr, "id_data": id_data})

# ==============================================================================
# C. Build feature matrix directly from the Arrow Table
#
#    All dummy expansion and imputation was done in Stata. covariates
#    contains only numeric, fully-imputed columns. We read each column
#    directly from Arrow into a pre-allocated float32 array.
# ==============================================================================
feature_cols   = covariates
feature_labels = covar_labels
n_features_total = len(feature_cols)

print("  Feature matrix : {} rows x {} cols (float32, {:.1f} GB)".format(
      nobs, n_features_total, nobs * n_features_total * 4 / 1e9))
print_ram("before X_all allocation")

X_all = np.empty((nobs, n_features_total), dtype=np.float32)

for col_idx, v in enumerate(feature_cols):
    X_all[:, col_idx] = table.column(v).to_numpy(
        zero_copy_only=False).astype(np.float32)

del table
gc.collect()

check_no_nans(X_all, label="X_all")
print_ram("after X_all built, table released", {"X_all": X_all})

# ==============================================================================
# D. Reorder rows so training observations are contiguous at the top.
#
#    WHY WE REORDER:
#    sklearn requires a NumPy array for rf.fit(). Boolean indexing in NumPy
#    (e.g. X_all[mask_train]) always creates a FULL COPY of the selected
#    rows. At 48M training rows x 91 features x 4 bytes = ~17.5 GB, this
#    copy would add 17.5 GB to peak RAM on top of the 33 GB X_all.
#
#    By reordering X_all so that training rows occupy the first n_train
#    positions, we can use a CONTIGUOUS SLICE (X_all[:n_train]) which is
#    a zero-copy VIEW — it shares the same underlying memory as X_all and
#    consumes zero additional RAM.
#
#    The reorder also shuffles y_arr, id_data, leavout_arr, and all masks
#    so they remain aligned with X_all. The predictions Parquet is keyed
#    on idvars (which are also reordered), so the Stata merge works
#    correctly regardless of row order.
#
#    REPLICABILITY:
#    The reorder is deterministic — np.where() returns indices in ascending
#    order, and np.concatenate preserves that order. Given the same input
#    data and the same mask_train, the reorder will always produce the
#    same row arrangement.
# ==============================================================================
mask_y_obs = ~np.isnan(y_arr)
mask_ho    = leavout_arr
mask_train = mask_y_obs & ~mask_ho

n_train = int(np.sum(mask_train))
print("  Training observations : " + str(n_train))

if n_train < 10:
    print("ERROR: Fewer than 10 training observations ({}).".format(n_train))
    sys.exit(1)

print("  Reordering rows (training first) to enable zero-copy slice ...")

train_indices = np.where(mask_train)[0]
other_indices = np.where(~mask_train)[0]
reorder       = np.concatenate([train_indices, other_indices])

X_all       = X_all[reorder]
y_arr       = y_arr[reorder]
id_data     = id_data.iloc[reorder].reset_index(drop=True)
leavout_arr = leavout_arr[reorder]
mask_y_obs  = mask_y_obs[reorder]
mask_ho     = mask_ho[reorder]

mask_train = np.zeros(nobs, dtype=bool)
mask_train[:n_train] = True

del train_indices, other_indices, reorder
gc.collect()

# X_train and y_train are zero-copy VIEWS — no additional RAM
X_train = X_all[:n_train]
y_train = y_arr[:n_train]

check_no_nans(X_train, label="X_train")

print_ram("after reorder (X_train is a view, not a copy)",
          {"X_all": X_all})

# ==============================================================================
# E. Determine hyperparameters
#
#    Three modes in priority order:
#      1. Manual: user specified maxfeatures and/or minleaf
#      2. Tune: calls rf_bgincome_tune.py via exec()
#      3. Default: sqrt rule + minleaf=5
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

elif do_tune:
    print("  Tuning: calling rf_bgincome_tune.py ...")
    tune_script = os.path.join(os.path.dirname(extract_path), "rf_bgincome_tune.py")
    if not os.path.isfile(tune_script):
        tune_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "rf_bgincome_tune.py"
        )
    if not os.path.isfile(tune_script):
        print("ERROR: rf_bgincome_tune.py not found.")
        sys.exit(1)
    with open(tune_script, "r") as f:
        exec(f.read())
    print("  Tuning complete.")
    print("  max_features              : " + str(best_params["max_features"]))
    print("  min_samples_leaf          : " + str(best_params["min_samples_leaf"]))

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

print_ram("before rf.fit", {"X_all": X_all})

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
print_ram("after rf.fit")

importances = pd.Series(rf.feature_importances_, index=feature_labels)
top10 = importances.nlargest(10)
print("  Top-10 feature importances:")
for fname, imp in top10.items():
    print("    {:<40s}  {:.4f}".format(fname, imp))

# ==============================================================================
# G. Generate predictions for all touse rows
#    X_all is already in memory — no reconstruction needed.
# ==============================================================================
print("  Generating predictions ...")
y_pred_all = rf.predict(X_all)

# Jensen's inequality correction for log outcome
if do_log:
    print("  Computing Jensen correction ...")
    y_train_pred = rf.predict(X_train)
    y_train_true = y_arr[:n_train]
    resid_var    = np.var(y_train_true - y_train_pred, ddof=1)
    y_pred_all_level = np.exp(y_pred_all + 0.5 * resid_var)
    del y_train_pred, y_train_true
    gc.collect()

# Holdout R2 — computed from y_pred_all which already exists.
# No additional rf.predict call, zero additional RAM.
if leavout > 0 and mask_ho.any():
    valid_ho = mask_ho & mask_y_obs
    if valid_ho.sum() > 0:
        y_ho_true = y_arr[valid_ho]
        y_ho_pred = y_pred_all[valid_ho]
        ss_res = np.sum((y_ho_true - y_ho_pred) ** 2)
        ss_tot = np.sum((y_ho_true - np.mean(y_ho_true)) ** 2)
        ho_r2  = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
        print("  Holdout R2    : {:.4f}".format(ho_r2))
        del y_ho_true, y_ho_pred
        gc.collect()

del X_all
gc.collect()
print_ram("after predictions, X_all released", {"y_pred_all": y_pred_all})

print("  Predicted outcome summary (all touse rows):")
print("    Mean : {:.4f}".format(np.mean(y_pred_all)))
print("    SD   : {:.4f}".format(np.std(y_pred_all)))
print("    Min  : {:.4f}".format(np.min(y_pred_all)))
print("    Max  : {:.4f}".format(np.max(y_pred_all)))

# ==============================================================================
# H. Write predictions to a small Parquet file keyed on idvars
#
#    id_data was reordered in section D to stay aligned with X_all.
#    The predictions are in the same reordered order. The Parquet file
#    contains idvars + predictions, and Stata merges on idvars, so row
#    order does not matter — the merge is key-based.
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
print_ram("after writing predictions", {"pred_df": pred_df})
print("  Done.")