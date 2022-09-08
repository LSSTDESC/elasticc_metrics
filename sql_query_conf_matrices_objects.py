import argparse
import sys
import os
import json
from pprint import pprint

import pandas as pd
import requests


def parse_args(args=None):
    parser = argparse.ArgumentParser(
        prog='Confusion matrices for the last broker classifications',
        description='Get confusion matrices for true and last predicted classes for each pair of diaObject and classifierId',
    )
    parser.add_argument('--include-missed', action='store_true', help='Add missed classifications as a predicted class')
    parser.add_argument('--plot', action='store_true', help='Plot and save confusion matrices')
    parser.add_argument('--save', action='store_true', help='Save CSV with data')
    return parser.parse_args(args)


def get_classifications(include_missed: bool = False):
    # Put in your TOM username and password here.  What I have here
    # reads the password from a private directory, so this code will not
    # work as is for you.  You just need to set the username and
    # password variables for use in the rqs.post call below.
    
    url = "https://desc-tom.lbl.gov"
    username = "kostya"

    # First, log in.  Because of Django's prevention against cross-site
    # scripting attacks, there is some dancing about you have to do in
    # order to make sure it always gets the right "csrftoken" header.

    rqs = requests.session()
    rqs.get( f'{url}/accounts/login/' )
    res = rqs.post( f'{url}/accounts/login/',
                    data={ "username": username,
                           "password": os.getenv("DESC_TOM_PASSWORD"),
                           "csrfmiddlewaretoken": rqs.cookies['csrftoken'] } )
    if res.status_code != 200:
        raise RuntimeError( f"Failed to log in; http status: {res.status_code}" )
    if 'Please enter a correct' in res.text:
        # This is a very cheesy attempt at checking if the login failed.
        # I haven't found clean documentation on how to log into a django site
        # from an app like this using standard authentication stuff.  So, for
        # now, I'm counting on the HTML that happened to come back when
        # I ran it with a failed login one time.  One of these days I'll actually
        # figure out how Django auth works and make a version of /accounts/login/
        # designed for use in API scripts like this one, rather than desgined
        # for interactive users.
        raise RuntimeError( "Failed to log in.  I think.  Put in a debug break and look at res.text" )
    rqs.headers.update( { 'X-CSRFToken': rqs.cookies['csrftoken'] } )

    # Next, send your query, passing the csrfheader with each request.
    # (The rqs.headers.update call above makes that happen if you just
    # keep using the rqs object.)
    #
    # You send the query as a json-encoded dictionary with two fields:
    #   'query' : the SQL query, with %(name)s for things that should
    #                be substituted.  (This is standard psycopg2.)
    #   'subdict' : a dictionary of substitutions for %(name)s things in your query
    #
    # The backend is to this web API call is readonly, so you can't
    # bobby tables this.  However, this does give you the freedom to
    # read anything from the tables if you know the schema.
    #
    # At some point in the near future, elasticc truth tables will be restricted
    # to elasticc admins.
    #
    # (Some relevant schema are at the bottom.brokerMessageId)
    
    # Note that I have to double-quote the column name beacuse otherwise
    #  Postgres converts things to lc for some reason or another.

    if include_missed:
        last_best_join_type = 'LEFT'
    else:
        last_best_join_type = 'INNER'

    query = f'''
        SELECT
        last_best."classId" AS pred_class,
        elasticc_gentypeofclassid."classId" AS true_class,
        elasticc_brokerclassifier."classifierId" as classifier_index,
        elasticc_brokerclassifier."brokerName" as broker_name,
        elasticc_brokerclassifier."classifierName" as classifier_name,
        avg(last_best."probability") AS mean_probability,
        max(last_best."probability") AS max_probability,
        min(last_best."probability") AS min_probability,
        count(*) AS n
            FROM elasticc_diaobject
            INNER JOIN elasticc_diaobjecttruth
                ON (elasticc_diaobject."diaObjectId" = elasticc_diaobjecttruth."diaObjectId")
            INNER JOIN elasticc_gentypeofclassid
                ON (elasticc_diaobjecttruth."gentype" = elasticc_gentypeofclassid."gentype")
            INNER JOIN (
                SELECT DISTINCT ON ("diaObjectId")
                "diaObjectId", "alertId", "diaSourceId", "alertSentTimestamp"
                    FROM elasticc_diaalert
                    ORDER BY "diaObjectId", "alertSentTimestamp" DESC NULLS LAST
                ) last_sent_alert
                ON (elasticc_diaobject."diaObjectId" = last_sent_alert."diaObjectId")
            CROSS JOIN elasticc_brokerclassifier
            {last_best_join_type} JOIN (
                SELECT
                last_broker_message."diaObjectId",
                best_classification."classifierId",
                best_classification."classId",
                best_classification."probability"
                FROM (
                    SELECT DISTINCT ON (elasticc_brokerclassification."classifierId", elasticc_diaalert."diaObjectId")
                    "diaObjectId",
                    elasticc_brokerclassification."brokerMessageId"
                        FROM elasticc_brokermessage
                        INNER JOIN elasticc_diaalert
                            ON (elasticc_brokermessage."alertId" = elasticc_diaalert."alertId")
                        INNER JOIN elasticc_brokerclassification
                            ON (elasticc_brokerclassification."brokerMessageId" = elasticc_brokermessage."brokerMessageId")
                        ORDER BY elasticc_brokerclassification."classifierId", elasticc_diaalert."diaObjectId", "elasticcPublishTimestamp" DESC
                    ) last_broker_message
                    INNER JOIN ( 
                        SELECT DISTINCT ON (elasticc_brokerclassification."brokerMessageId")
                        elasticc_brokerclassification."classId",
                        elasticc_brokerclassification."probability",
                        elasticc_brokerclassification."brokerMessageId",
                        elasticc_brokerclassifier."classifierId"
                            FROM elasticc_brokerclassification
                            INNER JOIN elasticc_brokerclassifier
                                USING ("classifierId")
                            ORDER BY elasticc_brokerclassification."brokerMessageId", elasticc_brokerclassification."probability" DESC
                        ) best_classification
                        ON (last_broker_message."brokerMessageId" = best_classification."brokerMessageId")
                ) last_best
                    ON (
                        elasticc_diaobject."diaObjectId" = last_best."diaObjectId"
                        AND elasticc_brokerclassifier."classifierId" = last_best."classifierId"
                    )
            WHERE last_sent_alert."alertSentTimestamp" IS NOT NULL
            GROUP BY pred_class, true_class, classifier_index
    '''
    result = rqs.post( f'{url}/db/runsqlquery/',
                       json={ 'query': query, 'subdict': {} } )

    # Look at the response.  It will be a JSON encoded dict with two fields:
    #  { 'status': 'ok',
    #    'rows': [...] }
    # where rows has the rows returned by the SQL query; each element of the row
    # is a dict.  There's probably a more efficient way to return this.  I'll
    # add formatting parameters later -- for instance, it might be nice to be able
    # to get a serialized pandas DataFrame, which then skips the binary-to-text-to-binary
    # translation that going through JSON will do.

    if result.status_code != 200:
        sys.stderr.write( f"ERROR: got status code {result.status_code} ({result.reason})\n" )
        return
    data = json.loads( result.text )
    if ( 'status' not in data ) or ( data['status'] != 'ok' ):
        sys.stderr.write( f"Got unexpected response:\n{data}\n" )
        return
    pprint(data['rows'])

    df = pd.DataFrame.from_records(data['rows'])
    # Replace missed classification with -1 label
    df['pred_class'] = df['pred_class'].fillna(-1).astype(int)

    return df


def plot_matrix(matrix):
    import matplotlib.pyplot as plt
    from sklearn.metrics import ConfusionMatrixDisplay

    plt.figure(figsize=(15, 15))
    row = matrix.iloc[0]
    name = f'{row["broker_name"]} {row["classifier_name"]}'
    cmd = ConfusionMatrixDisplay.from_predictions(
        y_true=matrix['true_class'],
        y_pred=matrix['pred_class'],
        sample_weight=matrix['n'],
        normalize='true',
        ax=plt.gca(),
        cmap='Blues',
    )
    cmd.ax_.get_images()[0].set_clim(0, 1)
    plt.title(name)
    plt.savefig(f'{name}.pdf')
    plt.close()


def main(cli_args=None):
    args = parse_args(cli_args)
    df = get_classifications(include_missed=args.include_missed)
    if args.save:
        df.to_csv('conf_matrices.csv', index=False)
    if args.plot:
        for classifier_id, matrix in df.groupby('classifier_index'):
            plot_matrix(matrix)



# ======================================================================
if __name__ == "__main__":
    main()


# ======================================================================
# elasticc tables as of 2022-08-05
#
# The order of the columns is what happens to be in the database, as a
# result of the specific history of django databse migrations.  It's not
# a sane order, alas.  You can find the same schema in the django source
# code, where the columns are in a more sane order; look at
# https://github.com/LSSTDESC/tom_desc/blob/main/elasticc/models.py

#             Table "public.elasticc_diaalert"
#     Column    |  Type  | Collation | Nullable | Default 
# --------------+--------+-----------+----------+---------
#  alertId      | bigint |           | not null | 
#  diaObjectId | bigint |           |          | 
#  diaSourceId | bigint |           |          | 
# Indexes:
#     "elasticc_diaalert_pkey" PRIMARY KEY, btree ("alertId")
#     "elasticc_diaalert_diaObjectId_809a8089" btree ("diaObjectId")
#     "elasticc_diaalert_diaSourceId_1f178060" btree ("diaSourceId")
# Foreign-key constraints:
#     "elasticc_diaalert_diaObjectId_809a8089_fk_elasticc_" FOREIGN KEY ("diaObjectId") REFERENCES elasticc_diaobject("diaObjectId") DEFERRABLE INITIALLY DEFERRED
#     "elasticc_diaalert_diaSourceId_1f178060_fk_elasticc_" FOREIGN KEY ("diaSourceId") REFERENCES elasticc_diasource("diaSourceId") DEFERRABLE INITIALLY DEFERRED
# Referenced by:
#     TABLE "elasticc_diaalertprvsource" CONSTRAINT "elasticc_diaalertprv_diaAlert_id_68c18d04_fk_elasticc_" FOREIGN KEY ("diaAlert_id") REFERENCES elasticc_diaalert("alertId") DEFERRABLE INITIALLY DEFERRED
#     TABLE "elasticc_diaalertprvforcedsource" CONSTRAINT "elasticc_diaalertprv_diaAlert_id_ddb308dc_fk_elasticc_" FOREIGN KEY ("diaAlert_id") REFERENCES elasticc_diaalert("alertId") DEFERRABLE INITIALLY DEFERRED
# 
#                     Table "public.elasticc_diaobject"
#         Column        |       Type       | Collation | Nullable | Default 
# ----------------------+------------------+-----------+----------+---------
#  diaObjectId          | bigint           |           | not null | 
#  ra                   | double precision |           | not null | 
#  decl                 | double precision |           | not null | 
#  mwebv                | double precision |           |          | 
#  mwebv_err            | double precision |           |          | 
#  z_final              | double precision |           |          | 
#  z_final_err          | double precision |           |          | 
#  hostgal_ellipticity  | double precision |           |          | 
#  hostgal_sqradius     | double precision |           |          | 
#  hostgal_zspec        | double precision |           |          | 
#  hostgal_zspec_err    | double precision |           |          | 
#  hostgal_zphot_q010   | double precision |           |          | 
#  hostgal_zphot_q020   | double precision |           |          | 
#  hostgal_zphot_q030   | double precision |           |          | 
#  hostgal_zphot_q040   | double precision |           |          | 
#  hostgal_zphot_q050   | double precision |           |          | 
#  hostgal_zphot_q060   | double precision |           |          | 
#  hostgal_zphot_q070   | double precision |           |          | 
#  hostgal_zphot_q080   | double precision |           |          | 
#  hostgal_zphot_q090   | double precision |           |          | 
#  hostgal_zphot_q100   | double precision |           |          | 
#  hostgal_mag_u        | double precision |           |          | 
#  hostgal_mag_g        | double precision |           |          | 
#  hostgal_mag_r        | double precision |           |          | 
#  hostgal_mag_i        | double precision |           |          | 
#  hostgal_mag_z        | double precision |           |          | 
#  hostgal_mag_Y        | double precision |           |          | 
#  hostgal_ra           | double precision |           |          | 
#  hostgal_dec          | double precision |           |          | 
#  hostgal_snsep        | double precision |           |          | 
#  hostgal_magerr_u     | double precision |           |          | 
#  hostgal_magerr_g     | double precision |           |          | 
#  hostgal_magerr_r     | double precision |           |          | 
#  hostgal_magerr_i     | double precision |           |          | 
#  hostgal_magerr_z     | double precision |           |          | 
#  hostgal_magerr_Y     | double precision |           |          | 
#  hostgal2_ellipticity | double precision |           |          | 
#  hostgal2_sqradius    | double precision |           |          | 
#  hostgal2_zphot       | double precision |           |          | 
#  hostgal2_zphot_err   | double precision |           |          | 
#  hostgal2_zphot_q010  | double precision |           |          | 
#  hostgal2_zphot_q020  | double precision |           |          | 
#  hostgal2_zphot_q030  | double precision |           |          | 
#  hostgal2_zphot_q040  | double precision |           |          | 
#  hostgal2_zphot_q050  | double precision |           |          | 
#  hostgal2_zphot_q060  | double precision |           |          | 
#  hostgal2_zphot_q070  | double precision |           |          | 
#  hostgal2_zphot_q080  | double precision |           |          | 
#  hostgal2_ellipticity | double precision |           |          | 
#  hostgal2_sqradius    | double precision |           |          | 
#  hostgal2_zphot       | double precision |           |          | 
#  hostgal2_zphot_err   | double precision |           |          | 
#  hostgal2_zphot_q010  | double precision |           |          | 
#  hostgal2_zphot_q020  | double precision |           |          | 
#  hostgal2_zphot_q030  | double precision |           |          | 
#  hostgal2_zphot_q040  | double precision |           |          | 
#  hostgal2_zphot_q050  | double precision |           |          | 
#  hostgal2_zphot_q060  | double precision |           |          | 
#  hostgal2_zphot_q070  | double precision |           |          | 
#  hostgal2_zphot_q080  | double precision |           |          | 
#  hostgal2_zphot_q090  | double precision |           |          | 
#  hostgal2_zphot_q100  | double precision |           |          | 
#  hostgal2_mag_u       | double precision |           |          | 
#  hostgal2_mag_g       | double precision |           |          | 
#  hostgal2_mag_r       | double precision |           |          | 
#  hostgal2_mag_i       | double precision |           |          | 
#  hostgal2_mag_z       | double precision |           |          | 
#  hostgal2_mag_Y       | double precision |           |          | 
#  hostgal2_ra          | double precision |           |          | 
#  hostgal2_dec         | double precision |           |          | 
#  hostgal2_snsep       | double precision |           |          | 
#  hostgal2_magerr_u    | double precision |           |          | 
#  hostgal2_magerr_g    | double precision |           |          | 
#  hostgal2_magerr_r    | double precision |           |          | 
#  hostgal2_magerr_i    | double precision |           |          | 
#  hostgal2_magerr_z    | double precision |           |          | 
#  hostgal2_magerr_Y    | double precision |           |          | 
#  simVersion           | text             |           |          | 
#  hostgal2_zphot_q000  | double precision |           |          | 
#  hostgal2_zspec       | double precision |           |          | 
#  hostgal2_zspec_err   | double precision |           |          | 
#  hostgal_zphot        | double precision |           |          | 
#  hostgal_zphot_err    | double precision |           |          | 
#  hostgal_zphot_p50    | double precision |           |          | 
#  hostgal_zphot_q000   | double precision |           |          | 
#  hostgal2_zphot_p50   | double precision |           |          | 
# Indexes:
#     "elasticc_diaobject_pkey" PRIMARY KEY, btree ("diaObjectId")
#     "idx_elasticc_diaobject_q3c" btree (q3c_ang2ipix(ra, decl))
# Referenced by:
#     TABLE "elasticc_diaalert" CONSTRAINT "elasticc_diaalert_diaObjectId_809a8089_fk_elasticc_" FOREIGN KEY ("diaObjectId") REFERENCES elasticc_diaobject("diaObjectId") DEFERRABLE INITIALLY DEFERRED
#     TABLE "elasticc_diaforcedsource" CONSTRAINT "elasticc_diaforcedso_diaObjectId_8b1bc498_fk_elasticc_" FOREIGN KEY ("diaObjectId") REFERENCES elasticc_diaobject("diaObjectId") DEFERRABLE INITIALLY DEFERRED
#     TABLE "elasticc_diaobjecttruth" CONSTRAINT "elasticc_diaobjecttr_diaObjectId_b5103ef2_fk_elasticc_" FOREIGN KEY ("diaObjectId") REFERENCES elasticc_diaobject("diaObjectId") DEFERRABLE INITIALLY DEFERRED
#     TABLE "elasticc_diasource" CONSTRAINT "elasticc_diasource_diaObjectId_3b88bc59_fk_elasticc_" FOREIGN KEY ("diaObjectId") REFERENCES elasticc_diaobject("diaObjectId") DEFERRABLE INITIALLY DEFERRED
# 
#                    Table "public.elasticc_diasource"
#       Column       |       Type       | Collation | Nullable | Default 
# -------------------+------------------+-----------+----------+---------
#  diaSourceId       | bigint           |           | not null | 
#  ccdVisitId        | bigint           |           | not null | 
#  parentDiaSourceId | bigint           |           |          | 
#  midPointTai       | double precision |           | not null | 
#  filterName        | text             |           | not null | 
#  ra                | double precision |           | not null | 
#  decl              | double precision |           | not null | 
#  psFlux            | double precision |           | not null | 
#  psFluxErr         | double precision |           | not null | 
#  snr               | double precision |           | not null | 
#  nobs              | double precision |           |          | 
#  diaObjectId      | bigint           |           |          | 
# Indexes:
#     "elasticc_diasource_pkey" PRIMARY KEY, btree ("diaSourceId")
#     "elasticc_diasource_diaObjectId_3b88bc59" btree ("diaObjectId")
#     "elasticc_diasource_midPointTai_5766b47f" btree ("midPointTai")
#     "idx_elasticc_diasource_q3c" btree (q3c_ang2ipix(ra, decl))
# Foreign-key constraints:
#     "elasticc_diasource_diaObjectId_3b88bc59_fk_elasticc_" FOREIGN KEY ("diaObjectId") REFERENCES elasticc_diaobject("diaObjectId") DEFERRABLE INITIALLY DEFERRED
# Referenced by:
#     TABLE "elasticc_diaalert" CONSTRAINT "elasticc_diaalert_diaSourceId_1f178060_fk_elasticc_" FOREIGN KEY ("diaSourceId") REFERENCES elasticc_diasource("diaSourceId") DEFERRABLE INITIALLY DEFERRED
#     TABLE "elasticc_diaalertprvsource" CONSTRAINT "elasticc_diaalertprv_diaSourceId_91fa84a3_fk_elasticc_" FOREIGN KEY ("diaSourceId") REFERENCES elasticc_diasource("diaSourceId") DEFERRABLE INITIALLY DEFERRED
# 
#                 Table "public.elasticc_diaforcedsource"
#       Column       |       Type       | Collation | Nullable | Default 
# -------------------+------------------+-----------+----------+---------
#  diaForcedSourceId | bigint           |           | not null | 
#  ccdVisitId        | bigint           |           | not null | 
#  midPointTai       | double precision |           | not null | 
#  filterName        | text             |           | not null | 
#  psFlux            | double precision |           | not null | 
#  psFluxErr         | double precision |           | not null | 
#  totFlux           | double precision |           | not null | 
#  totFluxErr        | double precision |           | not null | 
#  diaObjectId      | bigint           |           | not null | 
# Indexes:
#     "elasticc_diaforcedsource_pkey" PRIMARY KEY, btree ("diaForcedSourceId")
#     "elasticc_diaforcedsource_diaObjectId_8b1bc498" btree ("diaObjectId")
#     "elasticc_diaforcedsource_midPointTai_a80b03af" btree ("midPointTai")
# Foreign-key constraints:
#     "elasticc_diaforcedso_diaObjectId_8b1bc498_fk_elasticc_" FOREIGN KEY ("diaObjectId") REFERENCES elasticc_diaobject("diaObjectId") DEFERRABLE INITIALLY DEFERRED
# Referenced by:
#     TABLE "elasticc_diaalertprvforcedsource" CONSTRAINT "elasticc_diaalertprv_diaForcedSource_id_783ade34_fk_elasticc_" FOREIGN KEY ("diaForcedSource_id") REFERENCES elasticc_diaforcedsource("diaForcedSourceId") DEFERRABLE INITIALLY DEFERRED
# 
#                  Table "public.elasticc_diatruth"
#     Column    |       Type       | Collation | Nullable | Default 
# --------------+------------------+-----------+----------+---------
#  diaSourceId  | bigint           |           | not null | 
#  diaObjectId  | bigint           |           |          | 
#  detect       | boolean          |           |          | 
#  gentype | integer          |           |          | 
#  true_genmag  | double precision |           |          | 
#  mjd          | double precision |           |          | 
# Indexes:
#     "elasticc_diatruth_diaSourceId_648273bb_pk" PRIMARY KEY, btree ("diaSourceId")
#     "elasticc_diatruth_diaObjectId_7dd96889" btree ("diaObjectId")
#     "elasticc_diatruth_diaSourceId_648273bb_uniq" UNIQUE CONSTRAINT, btree ("diaSourceId")
# 
#                  Table "public.elasticc_diaobjecttruth"
#        Column       |       Type       | Collation | Nullable | Default 
# --------------------+------------------+-----------+----------+---------
#  libid              | integer          |           | not null | 
#  sim_searcheff_mask | integer          |           | not null | 
#  gentype            | integer          |           | not null | 
#  sim_template_index | integer          |           | not null | 
#  zcmb               | double precision |           | not null | 
#  zhelio             | double precision |           | not null | 
#  zcmb_smear         | double precision |           | not null | 
#  ra                 | double precision |           | not null | 
#  dec                | double precision |           | not null | 
#  mwebv              | double precision |           | not null | 
#  galid              | bigint           |           | not null | 
#  galzphot           | double precision |           | not null | 
#  galzphoterr        | double precision |           | not null | 
#  galsnsep           | double precision |           | not null | 
#  galsnddlr          | double precision |           | not null | 
#  rv                 | double precision |           | not null | 
#  av                 | double precision |           | not null | 
#  mu                 | double precision |           | not null | 
#  lensdmu            | double precision |           | not null | 
#  peakmjd            | double precision |           | not null | 
#  mjd_detect_first   | double precision |           | not null | 
#  mjd_detect_last    | double precision |           | not null | 
#  dtseason_peak      | double precision |           | not null | 
#  peakmag_u          | double precision |           | not null | 
#  peakmag_g          | double precision |           | not null | 
#  peakmag_r          | double precision |           | not null | 
#  peakmag_i          | double precision |           | not null | 
#  peakmag_z          | double precision |           | not null | 
#  peakmag_Y          | double precision |           | not null | 
#  snrmax             | double precision |           | not null | 
#  snrmax2            | double precision |           | not null | 
#  snrmax3            | double precision |           | not null | 
#  nobs               | integer          |           | not null | 
#  nobs_saturate      | integer          |           | not null | 
#  diaObjectId       | bigint           |           | not null | 
# Indexes:
#     "elasticc_diaobjecttruth_pkey" PRIMARY KEY, btree ("diaObjectId")
#     "elasticc_diaobjecttruth_gentype_480cd308" btree (gentype)
#     "elasticc_diaobjecttruth_sim_template_index_b33f9ab4" btree (sim_template_index)
# Foreign-key constraints:
#     "elasticc_diaobjecttr_diaObjectId_b5103ef2_fk_elasticc_" FOREIGN KEY ("diaObjectId") REFERENCES elasticc_diaobject("diaObjectId") DEFERRABLE INITIALLY DEFERRED
# 
#                                 Table "public.elasticc_gentypeofclassid"
#     Column     |  Type   | Collation | Nullable |                        Default                         
# ---------------+---------+-----------+----------+--------------------------------------------------------
#  id            | integer |           | not null | nextval('elasticc_gentypeofclassid_id_seq'::regclass)
#  classId       | integer |           | not null | 
#  gentype | integer |           |          | 
#  description   | text    |           | not null | 
# Indexes:
#     "elasticc_gentypeofclassid_pkey" PRIMARY KEY, btree (id)
#     "elasticc_gentypeofclassid_classId_27acf0d1" btree ("classId")
#     "elasticc_gentypeofclassid_gentype_63c19152" btree (gentype)
# 
# tom_desc=# \d elasticc_brokermessage
#                                                      Table "public.elasticc_brokermessage"
#           Column          |           Type           | Collation | Nullable |                             Default                              
# --------------------------+--------------------------+-----------+----------+------------------------------------------------------------------
#  brokerMessageId           | bigint                   |           | not null | nextval('"elasticc_brokermessage_brokerMessageId_seq"'::regclass)
#  streamMessageId          | bigint                   |           |          | 
#  topicName                | character varying(200)   |           |          | 
#  alertId                  | bigint                   |           | not null | 
#  diaSourceId              | bigint                   |           | not null | 
#  descIngestTimestamp      | timestamp with time zone |           | not null | 
#  elasticcPublishTimestamp | timestamp with time zone |           |          | 
#  brokerIngestTimestamp    | timestamp with time zone |           |          | 
#  modified                 | timestamp with time zone |           | not null | 
#  msgHdrTimestamp          | timestamp with time zone |           |          | 
# Indexes:
#     "elasticc_brokermessage_pkey" PRIMARY KEY, btree ("brokerMessageId")
#     "elasticc_br_alertId_b419c9_idx" btree ("alertId")
#     "elasticc_br_dbMessa_59550d_idx" btree ("brokerMessageId")
#     "elasticc_br_diaSour_ca3044_idx" btree ("diaSourceId")
#     "elasticc_br_topicNa_73f5a4_idx" btree ("topicName", "streamMessageId")
# Referenced by:
#     TABLE "elasticc_brokerclassification" CONSTRAINT "elasticc_brokerclass_brokerMessageId_b8bd04da_fk_elasticc_" FOREIGN KEY ("brokerMessageId") REFERENCES elasticc_brokermessage("brokerMessageId") DEFERRABLE INITIALLY DEFERRED
# 
#                                                    Table "public.elasticc_brokerclassifier"
#       Column       |           Type           | Collation | Nullable |                                Default                                 
# -------------------+--------------------------+-----------+----------+------------------------------------------------------------------------
#  classifierId | bigint                   |           | not null | nextval('"elasticc_brokerclassifier_classifierId_seq"'::regclass)
#  brokerName        | character varying(100)   |           | not null | 
#  brokerVersion     | text                     |           |          | 
#  classifierName    | character varying(200)   |           | not null | 
#  classifierParams  | text                     |           |          | 
#  modified          | timestamp with time zone |           | not null | 
# Indexes:
#     "elasticc_brokerclassifier_pkey" PRIMARY KEY, btree ("classifierId")
#     "elasticc_br_brokerN_38d99f_idx" btree ("brokerName", "classifierName")
#     "elasticc_br_brokerN_86cc1a_idx" btree ("brokerName")
#     "elasticc_br_brokerN_eb7553_idx" btree ("brokerName", "brokerVersion")
# Referenced by:
#     TABLE "elasticc_brokerclassification" CONSTRAINT "elasticc_brokerclass_classifierId_91d33318_fk_elasticc_" FOREIGN KEY ("classifierId") REFERENCES elasticc_brokerclassifier("classifierId") DEFERRABLE INITIALLY DEFERRED
# 
# 
#                                                        Table "public.elasticc_brokerclassification"
#         Column         |           Type           | Collation | Nullable |                                    Default                                     
# -----------------------+--------------------------+-----------+----------+--------------------------------------------------------------------------------
#  classificationId | bigint                   |           | not null | nextval('"elasticc_brokerclassification_classificationId_seq"'::regclass)
#  classId               | integer                  |           | not null | 
#  probability           | double precision         |           | not null | 
#  modified              | timestamp with time zone |           | not null | 
#  classifierId       | bigint                   |           |          | 
#  brokerMessageId          | bigint                   |           |          | 
# Indexes:
#     "elasticc_brokerclassification_pkey" PRIMARY KEY, btree ("classificationId")
#     "elasticc_brokerclassification_classifierId_91d33318" btree ("classifierId")
#     "elasticc_brokerclassification_brokerMessageId_b8bd04da" btree ("brokerMessageId")
# Foreign-key constraints:
#     "elasticc_brokerclass_classifierId_91d33318_fk_elasticc_" FOREIGN KEY ("classifierId") REFERENCES elasticc_brokerclassifier("classifierId") DEFERRABLE INITIALLY DEFERRED
#     "elasticc_brokerclass_brokerMessageId_b8bd04da_fk_elasticc_" FOREIGN KEY ("brokerMessageId") REFERENCES elasticc_brokermessage("brokerMessageId") DEFERRABLE INITIALLY DEFERRED