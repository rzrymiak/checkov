import os
import re
import logging
from http import HTTPStatus
from typing import List, Dict

import requests
from requests.exceptions import HTTPError

from checkov.common.models.consts import TFC_HOST_NAME
from checkov.common.goget.registry.get_registry import RegistryGetter
from checkov.terraform.module_loading.content import ModuleContent
from checkov.terraform.module_loading.loader import ModuleLoader
from checkov.terraform.module_loading.loaders.versions_parser import (
    order_versions_in_descending_order,
    get_version_constraints,
    VERSION_REGEX
)


class RegistryLoader(ModuleLoader):
    modules_versions_cache: Dict[str, List[str]] = {}

    def __init__(self) -> None:
        super().__init__()
        self.discover()
        self.module_version_url = ""
        self.best_version = ""

    def discover(self):
        self.REGISTRY_URL_PREFIX = os.getenv("REGISTRY_URL_PREFIX", "https://registry.terraform.io/v1/modules")
        self.token = os.getenv("TFC_TOKEN", "")

    def _is_matching_loader(self) -> bool:
        # Since the registry loader is the first one to be checked,
        # it shouldn't process any github modules
        if self.module_source.startswith(("github.com", "bitbucket.org", "git::")):
            return False
        self._process_inner_registry_module()
        if self.module_source.startswith(TFC_HOST_NAME):
            # indicates a private registry module
            self.REGISTRY_URL_PREFIX = f"https://{TFC_HOST_NAME}/api/registry/v1/modules"
            self.module_source = self.module_source.replace(f"{TFC_HOST_NAME}/", "")
        else:
            # url for the public registry
            self.REGISTRY_URL_PREFIX = "https://registry.terraform.io/v1/modules"

        if self.module_source.startswith(self.REGISTRY_URL_PREFIX):
            # TODO: implement registry url validation using remote service discovery
            # https://www.terraform.io/internals/remote-service-discovery#remote-service-discovery
            pass
        self.module_version_url = "/".join((self.REGISTRY_URL_PREFIX, self.module_source, "versions"))
        if not self.module_version_url.startswith(self.REGISTRY_URL_PREFIX):
            # Local paths don't get the prefix appended
            return False

        if self.module_version_url in RegistryLoader.modules_versions_cache.keys():
            return True
        if not self._cache_available_versions():
            return False
        # Determine the best version to use as per version constraints for finding accurate dest_dir.
        self.best_version = self._find_best_version()
        logging.debug(f"Best version for {self.module_source} is {self.best_version}")
        if not self.inner_module: 
            self.dest_dir = os.path.join(self.root_dir, self.external_modules_folder_name, TFC_HOST_NAME,
                                         *self.module_source.split("/"), self.best_version)
        if os.path.exists(self.dest_dir):
            return True
        # verify cache again after refresh
        if self.module_version_url in RegistryLoader.modules_versions_cache.keys():
            return True
        return False

    def _load_module(self) -> ModuleContent:
        if os.path.exists(self.dest_dir):
            return ModuleContent(dir=self.dest_dir)

        best_version = self.best_version
        request_download_url = "/".join((self.REGISTRY_URL_PREFIX, self.module_source, best_version, "download"))
        try:
            response = requests.get(url=request_download_url, headers={"Authorization": f"Bearer {self.token}"})
            response.raise_for_status()
        except HTTPError as e:
            self.logger.warning(e)
            if response.status_code != HTTPStatus.OK and response.status_code != HTTPStatus.NO_CONTENT:
                return ModuleContent(dir=None)
        else:
            # https://www.terraform.io/registry/api-docs#download-source-code-for-a-specific-module-version
            module_download_url = response.headers.get('X-Terraform-Get', '')
            self.logger.debug(f"Cloning module from: X-Terraform-Get: {module_download_url}")
            if module_download_url.startswith("https://archivist.terraform.io/v1/object"):
                try:
                    registry_getter = RegistryGetter(module_download_url)
                    registry_getter.temp_dir = self.dest_dir
                    registry_getter.do_get()
                    return_dir = self.dest_dir
                except Exception as e:
                    str_e = str(e)
                    if 'File exists' not in str_e and 'already exists and is not an empty directory' not in str_e:
                        self.logger.error(f"failed to get {self.module_source} because of {e}")
                        return ModuleContent(dir=None, failed_url=self.module_source)
                if self.inner_module:
                    return_dir = os.path.join(self.dest_dir, self.inner_module)
                return ModuleContent(dir=return_dir)
            else:
                return ModuleContent(dir=None, next_url=response.headers.get("X-Terraform-Get", ""))

    def _find_module_path(self) -> str:
        # to determine the exact path here would be almost a duplicate of the git_loader functionality
        return ""

    def _find_best_version(self) -> str:
        versions_by_size = RegistryLoader.modules_versions_cache.get(self.module_version_url, [])
        if self.version == "latest":
            self.version = versions_by_size[0]
        version_constraints = get_version_constraints(self.version)
        num_of_matches = 0
        for version in versions_by_size:
            for version_constraint in version_constraints:
                if not version_constraint.versions_matching(version):
                    break
                else:
                    num_of_matches += 1
            if num_of_matches == len(version_constraints):
                return version
            else:
                num_of_matches = 0
        return "latest"

    def _cache_available_versions(self) -> bool:
        # Get all available versions for a module in the registry and cache them.
        # Returns False on failure.
        try:
            response = requests.get(url=self.module_version_url, headers={"Authorization": f"Bearer {self.token}"})
            response.raise_for_status()
            available_versions = [
                v.get("version") for v in response.json().get("modules", [{}])[0].get("versions", {})
            ]
            RegistryLoader.modules_versions_cache[self.module_version_url] = order_versions_in_descending_order(
                available_versions)
            return True
        except HTTPError as e:
            self.logger.debug(e)
            return False

    def _process_inner_registry_module(self) -> None:
        # Check if the source has '//' in it. If it does, it indicates a reference for an inner module.
        # Example: "terraform-aws-modules/security-group/aws//modules/http-80" =>
        #    module_source = terraform-aws-modules/security-group/aws
        #    dest_dir = modules/http-80
        module_source_components = self.module_source.split("//")
        if len(module_source_components) > 1:
            self.module_source = module_source_components[0]
            self.dest_dir = self.dest_dir.split("//")[0]
            self.inner_module = module_source_components[1]


loader = RegistryLoader()
