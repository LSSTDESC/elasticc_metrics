# elasticc_metrics

Instructions for pulling information about the ELAsTiCC2 campaign and generating metrics of broker performance.

## Directly from the TOM

The TOM elasticc page at https://desc-tom.lbl.gov/elasticc2/ has some diagonostics and metrics pre-computed.  You need an account on the tom to load this page.  You can find the rate at which alerts were streamed during the ELAsTiCC2 campaign; the time delays between alert generation, broker classification, and TOM ingesting of broker classifications; broker classification completeness; and one version of confusion matrices for all broker classifiers.

## Pulling pre-aggregated broker message data via REST API:

See the Jupyter Notebook https://github.com/LSSTDESC/elasticc_metrics/blob/main/elasticc2_rest_metric_demo.ipynb for instructions and a demo.

## Directly querying the database

See the Jupyter Notebook (TODO: Rob, write notebook) for instructions and a demo.


---

# EVERYTHING BELOW IS OLD, USE WITH GREAT CAUTION

They may be OK for the original ELAsTiCC campaign, but not ELAsTiCC2.

# elasticc_metrics
Notebooks for evaluating ELAsTiCC Metrics

For instructions about connecting to the databse, see either
[`tom_query_demo.ipynb`](https://github.com/LSSTDESC/elasticc_metrics/blob/main/tom_query_demo.ipynb)
in this archive, or
[`sql_query_tom_db.py`](https://github.com/LSSTDESC/tom_desc/blob/main/sql_query_tom_db.py)
from the DESC TOM's github archive.  The latter file also has the relevant elasticc database schema.

### Probabilistic metrics

There is a database view, `elasticc_view_classifications_probmetrics`, that holds histograms of classification probabilities.  This view took a very long time to generate, but is fast to query (you can pull the whole table down in several seconds).  To use it, see `metric_querier.py`, and as an example, `avg_prob_vs_class_and_time.ipynb`.

### Confusion matrices

[`sql_query_conf_matrices_objects.py`](https://github.com/LSSTDESC/elasticc_metrics/blob/main/sql_query_conf_matrices_objects.py)

This script contains the SQL queries used to generate the confusion matrices for the classification reports.
It requires to set `DESC_TOM_USERNAME` and `DESC_TOM_PASSWORD` environment variables to connect to https://desc-tom.lbl.gov.

Each value of a matrix is represented as both a per-cent (see `--norm` bellow) and object count.
Supported options:

- `--plot` plots the confusion matrices to a working directory as PDF files
- `--save` saves the confusion matrices to a working directory as a single CSV file
- `--include-missed` adds "missed" predicted class to count how many objects were sent to a broker but have never been reported back
- `--norm=[true,pred,all]` sets normalisation for values shown in matrices, "true" normalizes over true class values (each row sums up to unity, diagonal is completeness), "pred" normalizes over predicted values (each column sumps up to unity, diagonal is purity), "all" normalizes over all values
- `--definition=[last_best,best]` changes the definition of an object classification, "best" is a class corresponded to the maximum probability over all classifications for all alerts, while "last_best" considers the most recent classified alert only.
- `--classifier_id=[INT]` selects a classifier by its ID, if not set, all classifiers are considered