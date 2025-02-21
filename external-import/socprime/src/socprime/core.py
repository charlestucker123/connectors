import os
import yaml
import time
import datetime
from typing import Any, Dict, List, Optional, Mapping
from pycti.connector.opencti_connector_helper import (
    OpenCTIConnectorHelper,
    get_config_variable,
)
from stix2 import Bundle, Indicator
from socprime.tdm_api_client import ApiClient


class SocprimeConnector:
    _DEFAULT_CONNECTOR_RUN_INTERVAL_SEC = 3600
    _STATE_LAST_RUN = "last_run"
    update_existing_data = True

    def __init__(self):
        config = self._read_configuration()
        self.helper = OpenCTIConnectorHelper(config)
        tdm_api_key = get_config_variable(
            "SOCPRIME_API_KEY", ["socprime", "api_key"], config
        )
        self.tdm_content_list_name = get_config_variable(
            "SOCPRIME_CONTENT_LIST_NAME", ["socprime", "content_list_name"], config
        )
        self._siem_type = get_config_variable(
            "SOCPRIME_SIEM_TYPE", ["socprime", "siem_type"], config
        )
        self.interval_sec = get_config_variable(
            env_var="SOCPRIME_INTERVAL_SEC",
            yaml_path=["socprime", "interval_sec"],
            config=config,
            isNumber=True,
            default=self._DEFAULT_CONNECTOR_RUN_INTERVAL_SEC,
        )
        self.tdm_api_client = ApiClient(api_key=tdm_api_key)

    @staticmethod
    def _read_configuration() -> Dict[str, str]:
        config_file_path = os.path.dirname(os.path.abspath(__file__)) + "/../config.yml"
        if not os.path.isfile(config_file_path):
            return {}
        return yaml.load(open(config_file_path), Loader=yaml.FullLoader)

    def get_siem_types(self) -> List[str]:
        if not self._siem_type:
            return []
        elif isinstance(self._siem_type, list):
            return self._siem_type
        else:
            return [x.strip() for x in str(self._siem_type).split(",")]

    @staticmethod
    def _current_unix_timestamp() -> int:
        return int(time.time())

    def _load_state(self) -> Dict[str, Any]:
        current_state = self.helper.get_state()
        if not current_state:
            return {}
        return current_state

    @staticmethod
    def _get_state_value(
        state: Optional[Mapping[str, Any]], key: str, default: Optional[Any] = None
    ) -> Any:
        if state is not None:
            return state.get(key, default)
        return default

    def _is_scheduled(self, last_run: Optional[int], current_time: int) -> bool:
        if last_run is None:
            self.helper.log_info("Connector first run")
            return True
        time_diff = current_time - last_run
        return time_diff >= self.interval_sec

    @classmethod
    def _sleep(cls, delay_sec: Optional[int] = None) -> None:
        time.sleep(delay_sec)

    @classmethod
    def convert_sigma_rule_to_indicator(
        cls, rule: dict, siem_types: Optional[List[str]] = None
    ) -> Indicator:
        sigma_body = cls._get_sigma_body_from_rule(rule)
        indicator_id = (
            f'indicator--{sigma_body["id"]}' if sigma_body.get("id") else None
        )
        return Indicator(
            id=indicator_id,
            type="indicator",
            name=rule["case"]["name"],
            description=rule.get("description"),
            pattern=rule["sigma"]["text"],
            pattern_type="sigma",
            labels=sigma_body.get("tags", [])
            + ["Sigma Author: " + sigma_body.get("author")],
            confidence=cls.convert_sigma_status_to_stix_confidence(
                sigma_level=sigma_body.get("status")
            ),
            external_references=cls._get_external_refs_from_rule(
                rule, siem_types=siem_types
            ),
        )

    @classmethod
    def _get_external_refs_from_rule(
        cls, rule: dict, siem_types: Optional[List[str]] = None
    ) -> List[dict]:
        res = []
        context_resources = rule.get("context_resources")
        if isinstance(context_resources, dict):
            for link_type, doc in context_resources.items():
                if isinstance(doc, dict) and isinstance(doc.get("links"), list):
                    for link in doc["links"]:
                        if link_type and link:
                            if link_type == "detection":
                                res.append(
                                    {
                                        "source_name": "Detection Sigma",
                                        "url": f"{link}#sigma",
                                    }
                                )
                                if siem_types:
                                    for siem_type in siem_types:
                                        res.append(
                                            {
                                                "source_name": f"Detection {siem_type}",
                                                "url": f"{link}#{siem_type}",
                                            }
                                        )
                            else:
                                link_type: str
                                res.append(
                                    {
                                        "source_name": link_type.upper()
                                        if link_type == "cve"
                                        else link_type.capitalize(),
                                        "url": link,
                                    }
                                )

        res.append({"source_name": "SOC Prime", "url": cls._get_link_to_socprime(rule)})
        return res

    @staticmethod
    def _get_sigma_body_from_rule(rule: dict) -> dict:
        return yaml.safe_load(rule["sigma"]["text"].replace("---", ""))

    @classmethod
    def _get_link_to_socprime(cls, rule: dict) -> str:
        body = cls._get_sigma_body_from_rule(rule)
        sigma_id = body["id"]
        return f"https://socprime.com/rs/rule/{sigma_id}"

    @staticmethod
    def convert_sigma_status_to_stix_confidence(sigma_level: str) -> Optional[int]:
        mapping = {
            "stable": 85,
            "test": 50,
            "experimental": 15,
            "deprecated": 0,
            "unsupported": 0,
        }
        return mapping.get(str(sigma_level).lower())

    def _get_available_siem_types(self, rule_ids: List[str]) -> Dict[str, List[str]]:
        res = {}
        if rule_ids:
            try:
                query = "case.id: (" + " OR ".join(rule_ids) + ")"
                for siem_type in self.get_siem_types():
                    rules = self.tdm_api_client.search_rules(
                        siem_type=siem_type, client_query_string=query
                    )
                    for rule in rules:
                        case_id = rule["case"]["id"]
                        if case_id not in res:
                            res[case_id] = []
                        res[case_id].append(siem_type)
            except Exception:
                self.helper.log_error("Error while getting availables siem types.")
        return res

    def _get_rules_from_content_list(self) -> List[dict]:
        try:
            return self.tdm_api_client.get_rules_from_content_list(
                content_list_name=self.tdm_content_list_name, siem_type="sigma"
            )
        except Exception as err:
            self.helper.log_error(
                f"Error while getting rules from content list - {err}"
            )
            return []

    def send_rules_from_tdm(self, work_id: str) -> None:
        bundle_objects = []
        rules = self._get_rules_from_content_list()
        available_siem_types = self._get_available_siem_types(
            rule_ids=[x["case"]["id"] for x in rules]
        )

        for rule in rules:
            try:
                indicator = self.convert_sigma_rule_to_indicator(
                    rule, siem_types=available_siem_types.get(rule["case"]["id"])
                )
                bundle_objects.append(indicator)
            except Exception as err:
                case_id = rule.get("case", {}).get("id")
                self.helper.log_error(f"Error while parsing rule {case_id} - {err}")

        self.helper.log_info(f"Sending {len(bundle_objects)} rules")
        if bundle_objects:
            serialized_bundle = Bundle(objects=bundle_objects).serialize()
            self.helper.send_stix2_bundle(
                serialized_bundle, update=self.update_existing_data, work_id=work_id
            )

    def run(self):
        self.helper.log_info("Starting SOC Prime connector...")
        while True:
            self.helper.log_info("Running SOC Prime connector...")
            run_interval = self.interval_sec

            try:
                timestamp = self._current_unix_timestamp()
                current_state = self._load_state()

                self.helper.log_info(f"Loaded state: {current_state}")

                last_run = self._get_state_value(current_state, self._STATE_LAST_RUN)
                if self._is_scheduled(last_run, timestamp):
                    now = datetime.datetime.utcfromtimestamp(timestamp)
                    friendly_name = "SOC Prime run @ " + now.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    work_id = self.helper.api.work.initiate_work(
                        self.helper.connect_id, friendly_name
                    )

                    self.send_rules_from_tdm(work_id)

                    new_state = current_state.copy()
                    new_state[self._STATE_LAST_RUN] = self._current_unix_timestamp()

                    self.helper.log_info(f"Storing new state: {new_state}")
                    self.helper.set_state(new_state)
                    message = (
                        "State stored, next run in: "
                        + str(self.interval_sec)
                        + " seconds"
                    )
                    self.helper.api.work.to_processed(work_id, message)
                    self.helper.log_info(message)
                else:
                    next_run = self.interval_sec - (timestamp - last_run)
                    run_interval = min(run_interval, next_run)

                    self.helper.log_info(
                        f"Connector will not run, next run in: {next_run} seconds"
                    )

                self._sleep(delay_sec=run_interval)
            except (KeyboardInterrupt, SystemExit):
                self.helper.log_info("Connector stop")
                exit(0)
