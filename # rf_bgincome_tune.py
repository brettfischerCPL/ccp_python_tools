# rf_bgincome_tune.py
# Hyperparameter tuning for rf_bgincome.py via RandomizedSearchCV.
#
# This script is called via exec() from rf_bgincome.py when the user
# specifies the tune option. It expects the following variables to be
# available in the calling namespace:
#
#   X_train   : float32 NumPy array (training features, view of X_all)
#   y_train   : float64 NumPy array (training outcome, view of y_arr)
#   n_train   : int (number of training observations)
#   sqrt_frac : float (sqrt(n_features) / n_features)
#
# It sets:
#   best_params : dict with keys "max_features" and "min_samples_leaf"
#
# Required packages (already imported by caller):
#   numpy, scikit-learn, joblib

from sklearn.model_selection import RandomizedSearchCV, KFold

print("  Tuning hyperparameters via RandomizedSearchCV ...")

_max_leaf = max(6, int(n_train * 0.01))
_min_samples_leaf_grid = sorted(set(
    int(x) for x in
    np.unique(np.logspace(np.log10(5), np.log10(_max_leaf), num=20).astype(int))
))
_max_features_grid = sorted(set([sqrt_frac, 0.2, 0.3, 0.5]))

_param_dist = {
    "min_samples_leaf": _min_samples_leaf_grid,
    "max_features"    : _max_features_grid,
}

print("  min_samples_leaf candidates: " + str(_min_samples_leaf_grid))
print("  max_features candidates    : " + str(_max_features_grid))

_base_rf = RandomForestRegressor(
    n_estimators=100,
    random_state=42,
    n_jobs=1,
    oob_score=False,
)

_cv_splitter = KFold(n_splits=5, shuffle=True, random_state=42)

_search = RandomizedSearchCV(
    estimator           = _base_rf,
    param_distributions = _param_dist,
    n_iter              = 20,
    scoring             = "r2",
    cv                  = _cv_splitter,
    random_state        = 42,
    n_jobs              = -1,
    verbose             = 1,
    refit               = False,
)

with joblib.parallel_backend("threading"):
    _search.fit(X_train, y_train)

best_params = _search.best_params_
_best_cv_r2 = _search.best_score_

print("  Best min_samples_leaf : " + str(best_params["min_samples_leaf"]))
print("  Best max_features     : " + str(best_params["max_features"]))
print("  Best CV R2            : {:.4f}".format(_best_cv_r2))

del _search, _base_rf, _cv_splitter, _param_dist
del _min_samples_leaf_grid, _max_features_grid, _max_leaf, _best_cv_r2
gc.collect()