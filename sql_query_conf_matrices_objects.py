import argparse
import logging
import os
from pprint import pformat
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import requests


def parse_args(args=None):
    parser = argparse.ArgumentParser(
        prog='Confusion matrices for the last broker classifications',
        description=('Get confusion matrices for true and last predicted classes for '
                     'each pair of diaObject and classifierId'),
    )
    parser.add_argument('--include-missed', action='store_true',
                        help='Add missed classifications as a predicted class')
    parser.add_argument('--plot', action='store_true', help='Plot and save confusion matrices')
    parser.add_argument('--plotfmt', default='pdf', help='Format of saved plot (default: pdf)' )
    parser.add_argument('--save', action='store_true', help='Save CSV with data')
    parser.add_argument('--norm', default='true', choices=['true', 'pred', 'all'],
                        help='how t onormalize confusion matrices')
    parser.add_argument('--definition', default='last_best', choices=['last_best', 'best', 'nth'],
                        help='''definition of classification:
                            "best" means having the largest probability over all classifier messages,
                            "last_best" means having the largest probability for the most recent alert,
                            "nth" mean probability for the alert on the nth detect (use -n)
                        ''')
    parser.add_argument('-n', '--nth-detection', default=3, type=int,
                        help='Which detection to use for --definition nth' )
    parser.add_argument('--classifier_id', type=int, help='consider a single classifier')
    return parser.parse_args(args)


class ConfMatrixClient:
    url = "https://desc-tom.lbl.gov"

    @classmethod
    def from_credentials(cls, user, password):
        session = requests.session()
        session.get(f'{cls.url}/accounts/login/')
        res = session.post(
            f'{cls.url}/accounts/login/',
            data={
                "username": user,
                "password": password,
                "csrfmiddlewaretoken": session.cookies['csrftoken']
            },
        )
        if res.status_code != 200:
            raise RuntimeError(f"Failed to log in; http status: {res.status_code}")
        if 'Please enter a correct' in res.text:
            # This is a very cheesy attempt at checking if the login failed.
            # I haven't found clean documentation on how to log into a django site
            # from an app like this using standard authentication stuff.  So, for
            # now, I'm counting on the HTML that happened to come back when
            # I ran it with a failed login one time.  One of these days I'll actually
            # figure out how Django auth works and make a version of /accounts/login/
            # designed for use in API scripts like this one, rather than desgined
            # for interactive users.
            raise RuntimeError("Failed to log in.  I think.  Put in a debug break and look at res.text")
        session.headers['X-CSRFToken'] = session.cookies['csrftoken']
        return cls(session)

    def __init__(self, session: requests.Session):
        self.session = session
        self.taxonomy = { -1: 'missed' }
        self.load_taxonomy()
        self.load_classifiers()
        
    def load_taxonomy(self):
        query = ( 'SELECT DISTINCT ON ("classId") "classId",description '
                  'FROM elasticc_gentypeofclassid GROUP BY "classId",description' )
        data = self.query( query )
        tmp = { i['classId']: i['description'] for i in data }
        # The nature of the taxonomy is such that we don't want to sort
        # it numerically.  More digits means a subclass, and we want
        # subclasses sorted after their class.  Effectively, this means
        # that we can right-zero pad everything out to the same number
        # of digits and then sort that numerically.
        maxdigits = max( [ len(str(i)) for i in tmp.keys() ] )
        tmp = dict( sorted( tmp.items(), key = lambda x: int( ( str(x[0]) + '0'*maxdigits )[0:maxdigits] ) ) )
        self.taxonomy.update( tmp )

    def load_classifiers(self):
        query = f'''
           SELECT * FROM elasticc_brokerclassifier
           ORDER BY "brokerName", "brokerVersion", "classifierName", "classifierId"
        '''
        data = self.query(query)
        logging.debug(pformat(data))
        self.classifiers = {row['classifierId']: f'{row["brokerName"]} {row["brokerVersion"]} {row["classifierName"]}'
                            for row in data}
        
    def query(self, query: str) -> List[Dict]:
        result = self.session.post(f'{self.url}/db/runsqlquery/', json={'query': query, 'subdict': {}})
        result.raise_for_status()
        data = result.json()
        if ('status' not in data) or (data['status'] != 'ok'):
            raise RuntimeError(f"Got unexpected response:\n{data}\n")
        return data['rows']


    def get_classifications(self, *,
                            definition: str,
                            nth_detection: int = 3,
                            classifier_id: Optional[int],
                            include_missed: bool = False) -> Dict[str, pd.DataFrame]:
        if include_missed:
            best_last_join_type = 'LEFT'
            join_object_sent = '''
                INNER JOIN (
                    SELECT
                    "diaObjectId", bool_or("alertSentTimestamp" IS NOT NULL) AS "is_sent"
                        FROM elasticc_diaalert
                        GROUP BY "diaObjectId" 
                    ) object_sent_record
                        ON (elasticc_diaobjecttruth."diaObjectId" = object_sent_record."diaObjectId")
                    '''
            where = 'WHERE object_sent_record."is_sent"'
        else:
            best_last_join_type = 'INNER'
            join_object_sent = ''
            where = ''

        if definition == 'last_best':
            distinct_order = ( 'elasticc_diaalert."alertSentTimestamp" DESC,'
                               'elasticc_brokerclassification."probability" DESC' )
            count_join = ''
        elif definition == 'best':
            # I think we need additional sorting over alertSentTimestamp to get the deterministic result for the
            # case for equal probabilities (I've seen prob of 1.0)
            distinct_order = ( 'elasticc_brokerclassification."probability" DESC,'
                               'elasticc_diaalert."alertSentTimestamp" DESC' )
            count_join = ''
        elif definition == 'nth':
            distinct_order = 'elasticc_brokerclassification."probability" DESC'
            # Yeah, yeah, scary to interpolate a value passed to a function directly
            #  into SQL, but force it to be an int so that it will be safe.
            nth_detection = int( nth_detection )
            count_join = ( f'INNER JOIN elasticc_view_prevsourcecounts '
                           f'ON elasticc_diaalert."diaSourceId"=elasticc_view_prevsourcecounts."diaSourceId" '
                           f'  AND elasticc_view_prevsourcecounts.ndetections={nth_detection}' )
        else:
            raise ValueError(f'Unknown classification definition: {definition}')

        dfs = {}
        for classifier_id_, classifier_name in self.classifiers.items():
            if classifier_id is not None and classifier_id != classifier_id_:
                continue

            logging.info(f'Getting classifications for {classifier_name}...')
            query = f'''
                SELECT best_last."classId" AS pred_class,
                       elasticc_gentypeofclassid."classId" AS true_class,
                       COUNT(*) AS n
                FROM elasticc_diaobjecttruth
                INNER JOIN elasticc_gentypeofclassid
                    ON (elasticc_diaobjecttruth.gentype = elasticc_gentypeofclassid.gentype)
                {join_object_sent}
                {best_last_join_type} JOIN
                (
                   SELECT DISTINCT ON (elasticc_diaalert."diaObjectId")
                      elasticc_brokerclassification."classId", elasticc_brokerclassification."probability",
                      elasticc_diaalert."diaObjectId"
                   FROM elasticc_brokerclassification
                   INNER JOIN elasticc_brokermessage
                      ON elasticc_brokerclassification."brokerMessageId"=elasticc_brokermessage."brokerMessageId"
                   INNER JOIN elasticc_diaalert
                      ON elasticc_brokermessage."alertId"=elasticc_diaalert."alertId"
                   {count_join}
                   WHERE elasticc_brokerclassification."classifierId"={classifier_id_}
                   ORDER BY elasticc_diaalert."diaObjectId", {distinct_order}
                ) best_last
                ON (best_last."diaObjectId" = elasticc_diaobjecttruth."diaObjectId")
                {where}
                GROUP BY pred_class, true_class
                ORDER BY pred_class, true_class
            '''
            data = self.query(query)
            logging.info(pformat(data))
            if len(data) == 0:
                logging.warning(f'No data for {classifier_name}')
                continue
            df = pd.DataFrame.from_records(data)
            df['classifier_id'] = classifier_id_
            df['classifier_name'] = classifier_name
            df['pred_class'] = df['pred_class'].fillna(-1).astype(int)
            dfs[classifier_id_] = df

        logging.info('...done getting all classifications')
        return dfs


    @np.vectorize
    def conf_annotation(count: int, fraction: float) -> str:
        percent = np.round(fraction * 100)
        if count < 1_000_000:
            count_str = str(count)
        else:
            count_str = f'{count:.3g}'
        return f'{percent}%\n{count_str}'

    def plot_matrix(self, matrix: pd.DataFrame, *, norm: str, extension:str="pdf", show:bool=False ):
        import matplotlib.pyplot as plt
        import seaborn as sns
        from matplotlib.patches import Rectangle
        from sklearn.metrics import confusion_matrix

        plt.figure(figsize=(20, 20))
        plt.gca().set_aspect(
            # aspect=len(np.unique(matrix['true_class'])) / len(np.unique(matrix['pred_class'])),
            aspect='equal',
            adjustable='box',
        )
        name = matrix.iloc[0]['classifier_name']
        counts = confusion_matrix(
            y_true=matrix['true_class'],
            y_pred=matrix['pred_class'],
            sample_weight=matrix['n'],
            normalize=None,
        )
        # Remove empty lines corresponding to missed and Other classes
        idx = np.where(np.sum(counts, axis=1) > 0)[0], np.where(np.sum(counts, axis=0) > 0)[0]
        counts = counts[idx[0], :][:, idx[1]]
        fractions = confusion_matrix(
            y_true=matrix['true_class'],
            y_pred=matrix['pred_class'],
            sample_weight=matrix['n'],
            normalize=norm,
        )[idx[0], :][:, idx[1]]
        annotations = self.conf_annotation(counts, fractions)
        true_labels = np.vectorize(self.taxonomy.get)(np.unique(matrix['true_class']))
        pred_labels = np.vectorize(self.taxonomy.get)(np.unique(matrix['pred_class']))
        sns.heatmap(fractions,
                    cmap='Blues', vmin=0, vmax=1,
                    annot=annotations, fmt='s', annot_kws={"fontsize": 10},
                    xticklabels=pred_labels, yticklabels=true_labels)
        for j, label in enumerate(true_labels):
            try:
                i = np.where(pred_labels == label)[0].item()
            except ValueError:
                logging.warning(f'{label} not found in predictions for {name}')
                continue
            plt.gca().add_patch(Rectangle((i, j), 1, 1, ec='black', fc='none', lw=2))
        plt.title(name)
        plt.xlabel('Predicted class')
        plt.ylabel('True class')
        plt.tight_layout()
        if extension is not None:
            plt.savefig(f'{name}.{extension}')
        if show:
            plt.show()
        plt.close()

def main(cli_args=None):
    args = parse_args(cli_args)
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s - %(levelname)s] %(message)s' )
    username = os.getenv("DESC_TOM_USERNAME", "kostya")
    password = os.getenv("DESC_TOM_PASSWORD")
    client = ConfMatrixClient.from_credentials(username, password)
    dfs = client.get_classifications(definition=args.definition, classifier_id=args.classifier_id,
                                     include_missed=args.include_missed)
    if args.save:
        df = pd.concat(list(dfs.values()))
        df.to_csv('conf_matrices.csv', index=False)
    if args.plot:
        for classifier_id, matrix in dfs.items():
            client.plot_matrix(matrix, norm=args.norm, extension=args.plotfmt )



# ======================================================================
if __name__ == "__main__":
    main()


# ======================================================================
# elasticc tables as of 2022-08-05
#  (Removed this so that we don't have to worry about
#   keeping yet another thing in sync.)
