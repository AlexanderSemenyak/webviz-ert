import json
from typing import Any, Mapping, Optional, List, MutableMapping, Tuple, Dict
from collections import defaultdict
from pprint import pformat
import requests
import logging
import pandas as pd
import io

logger = logging.getLogger()

connection_info_map: dict = {}


def get_connection_info(project_id: str = None) -> Mapping[str, str]:
    from ert_shared.storage.connection import get_info

    if project_id not in connection_info_map:
        info = get_info(project_id)
        info["auth"] = info["auth"][1]
        connection_info_map[project_id] = info

    return connection_info_map[project_id]


# these are needed to mock for testing
def _requests_get(*args: Any, **kwargs: Any) -> requests.models.Response:
    return requests.get(*args, **kwargs)


def _requests_post(*args: Any, **kwargs: Any) -> requests.models.Response:
    return requests.post(*args, **kwargs)


GET_REALIZATION = """\
query($ensembleId: ID!) {
  ensemble(id: $ensembleId) {
    name
    responses {
      name
      data_uri
    }
    parameters {
      name
      data_uri
    }
  }
}
"""

GET_ALL_ENSEMBLES = """\
query {
  experiments {
    name
    ensembles {
      id
      timeCreated
      parentEnsemble {
        id
      }
      childEnsembles {
        id
      }
    }
  }
}
"""

GET_ENSEMBLE = """\
query ($id: ID!) {
  ensemble(id: $id) {
    id
    size
    activeRealizations
    timeCreated
    children {
      ensembleResult{
        id
      }
    }
    userdata
    parent {
      ensembleReference{
        id
      }
    }
    experiment {
      id
      name
    }
  }
}
"""

GET_PRIORS = """\
query($id: ID!) {
  experiment(id: $id) {
    priors
  }
}
"""


data_cache: dict = {}
ServerIdentifier = Tuple[str, Optional[str]]  # (baseurl, optional token)


class DataLoaderException(Exception):
    pass


class DataLoader:
    _instances: MutableMapping[ServerIdentifier, "DataLoader"] = {}

    baseurl: str
    token: Optional[str]
    _graphql_cache: MutableMapping[str, MutableMapping[dict, Any]]

    def __new__(cls, baseurl: str, token: Optional[str] = None) -> "DataLoader":
        if (baseurl, token) in cls._instances:
            return cls._instances[(baseurl, token)]

        loader = super().__new__(cls)
        loader.baseurl = baseurl
        loader.token = token
        loader._graphql_cache = defaultdict(dict)
        cls._instances[(baseurl, token)] = loader
        return loader

    def _query(self, query: str, **kwargs: Any) -> dict:
        """
        Cachable GraphQL helper
        """
        # query_cache = self._graphql_cache[query].get(kwargs)
        # if query_cache is not None:
        #     return query_cache
        resp = _requests_post(
            f"{self.baseurl}/gql",
            json={
                "query": query,
                "variables": kwargs,
            },
            headers={"Token": self.token},
        )
        try:
            doc = resp.json()
        except json.JSONDecodeError:
            doc = resp.content
        if resp.status_code != 200 or isinstance(doc, bytes):
            raise RuntimeError(
                f"ERT Storage query returned with '{resp.status_code}':\n{pformat(doc)}"
            )
        return doc["data"]

    def _get(
        self, url: str, headers: dict = None, params: dict = None
    ) -> requests.Response:
        if headers is None:
            headers = {}

        resp = _requests_get(
            f"{self.baseurl}/{url}",
            headers={**headers, "Token": self.token},
            params=params,
        )
        if resp.status_code != 200:
            raise DataLoaderException(
                f"""Error fetching data from {self.baseurl}/{url}
                The request return with status code: {resp.status_code}
                {str(resp.content)}
                """
            )
        return resp

    def get_all_ensembles(self) -> list:
        try:
            experiments = self._query(GET_ALL_ENSEMBLES)["experiments"]
            return [
                {"name": exp["name"], **ens}
                for exp in experiments
                for ens in exp["ensembles"]
            ]
        except RuntimeError as e:
            logger.error(e)
            return list()

    def get_ensemble(self, ensemble_id: str) -> dict:
        try:
            return self._query(GET_ENSEMBLE, id=ensemble_id)["ensemble"]
        except RuntimeError as e:
            logger.error(e)
            return dict()

    def get_ensemble_responses(self, ensemble_id: str) -> dict:
        try:
            return self._get(url=f"ensembles/{ensemble_id}/responses").json()
        except DataLoaderException as e:
            logger.error(e)
            return dict()

    def get_ensemble_userdata(self, ensemble_id: str) -> dict:
        try:
            return self._get(url=f"ensembles/{ensemble_id}/userdata").json()
        except DataLoaderException as e:
            logger.error(e)
            return dict()

    def get_ensemble_parameters(self, ensemble_id: str) -> list:
        try:
            return self._get(url=f"ensembles/{ensemble_id}/parameters").json()
        except DataLoaderException as e:
            logger.error(e)
            return list()

    def get_record_labels(self, ensemble_id: str, name: str) -> list:
        try:
            return self._get(
                url=f"ensembles/{ensemble_id}/records/{name}/labels"
            ).json()
        except DataLoaderException as e:
            logger.error(e)
            return list()

    def get_experiment_priors(self, experiment_id: str) -> dict:
        try:
            return json.loads(
                self._query(GET_PRIORS, id=experiment_id)["experiment"]["priors"]
            )
        except RuntimeError as e:
            logger.error(e)
            return dict()

    def get_ensemble_parameter_data(
        self,
        ensemble_id: str,
        parameter_name: str,
    ) -> pd.DataFrame:
        try:
            if "::" in parameter_name:
                name, label = parameter_name.split("::", 1)
                params = {"label": label}
            else:
                name = parameter_name
                params = {}

            resp = self._get(
                url=f"ensembles/{ensemble_id}/records/{name}",
                headers={"accept": "application/x-parquet"},
                params=params,
            )
            stream = io.BytesIO(resp.content)
            df = pd.read_parquet(stream).transpose()
            return df
        except DataLoaderException as e:
            logger.error(e)
            return pd.DataFrame()

    def get_ensemble_record_data(
        self,
        ensemble_id: str,
        record_name: str,
    ) -> pd.DataFrame:
        try:
            resp = self._get(
                url=f"ensembles/{ensemble_id}/records/{record_name}",
                headers={"accept": "application/x-parquet"},
            )
            stream = io.BytesIO(resp.content)
            df = pd.read_parquet(stream).transpose()

        except DataLoaderException as e:
            logger.error(e)
            return pd.DataFrame()

        try:
            df.index = df.index.astype(int)
        except TypeError:
            pass
        df = df.sort_index()
        return df

    def get_ensemble_record_observations(
        self, ensemble_id: str, record_name: str
    ) -> List[dict]:
        try:
            return self._get(
                url=f"ensembles/{ensemble_id}/records/{record_name}/observations",
                # Hard coded to zero, as all realizations are connected to the same observations
                params={"realization_index": 0},
            ).json()
        except DataLoaderException as e:
            logger.error(e)
            return list()

    def compute_misfit(
        self, ensemble_id: str, response_name: str, summary: bool
    ) -> pd.DataFrame:
        try:
            resp = self._get(
                "compute/misfits",
                params={
                    "ensemble_id": ensemble_id,
                    "response_name": response_name,
                    "summary_misfits": summary,
                },
            )
            stream = io.BytesIO(resp.content)
            df = pd.read_csv(stream, index_col=0, float_precision="round_trip")
            return df
        except DataLoaderException as e:
            logger.error(e)
            return pd.DataFrame()


def get_data_loader(project_id: Optional[str] = None) -> DataLoader:
    return DataLoader(*(get_connection_info(project_id).values()))


def get_ensembles(project_id: Optional[str] = None) -> list:
    return get_data_loader(project_id).get_all_ensembles()
