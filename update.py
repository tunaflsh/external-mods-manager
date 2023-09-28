#!/usr/bin/env python

import argparse
import json
import logging
import logging.config
import os
import re
import shutil
from urllib.parse import unquote

import requests

from logging_config import logging_config

MODSJSON = "mods.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-d", "--debug", help="Enable debug logging", action="store_true"
    )
    args = parser.parse_args()

    logging.config.dictConfig(logging_config("DEBUG" if args.debug else "INFO"))

    logger = logging.getLogger()

    modlist = json.load(open(MODSJSON, "r"))
    version = modlist["version"]
    mods = modlist["mods"]

    logger.info(
        "Updating %s for Minecraft %s",
        f"{len(mods)} mods" if len(mods) > 1 else "1 mod",
        version,
    )

    extractors = [
        extractor_factory(mod["name"], version, mod["source"], mod.get("file"))
        for mod in mods
    ]

    mods = {}
    for extractor in extractors:
        mods[extractor.name] = {
            "name": extractor.name,
            "source": extractor.source,
        }
        if extractor.download_jar():
            mods[extractor.name]["version"] = extractor.version
            mods[extractor.name]["file"] = extractor.file

    modlist["mods"] = list(mods.values())
    json.dump(modlist, open(MODSJSON, "w"), indent=4)

    logger.info("Done!")


def extractor_factory(
    name: str, version: str, source: str, file: str = None
) -> "ModExtractor":
    if name == "SeedcrackerX":
        return SeedcrackerXExtractor(version, source, file)
    else:
        raise NotImplementedError(f"Extractor for {name} not implemented")


class ModExtractor:
    def __init__(self, name: str, version: str, source: str, file: str = None):
        self.name = name
        self.version = version
        self.source = source
        self.file = file
        self.logger = logging.getLogger(self.name)

    def __repr__(self) -> str:
        return self.name

    def extract_jar(self) -> dict[str, str]:
        return {"version": None, "url": None}

    def download_jar(self) -> bool:
        """
        Returns:
            bool: True if the file was updated, False otherwise.
        """
        jar = self.extract_jar()
        if not jar:
            return False

        self.version = jar["version"]
        jar_url = jar["url"]

        response = requests.head(jar_url, allow_redirects=True)
        if response.status_code != 200:
            self.logger.error(
                f"Filename Header: Status Code {response.status_code} from {jar_url}"
            )
            return False

        self.logger.debug(f"Filename Header: {len(response.content)} bytes received")

        content_disposition = response.headers.get("Content-Disposition", "")

        if "filename=" not in content_disposition:
            self.logger.error(f"No filename")
            return False

        file = content_disposition.split("filename=")[1].strip('"')
        file = unquote(file)
        old_file, self.file = self.file, file

        if os.path.exists(file):
            self.logger.info(f"Already up to date")
            return False

        self.logger.info(f"Downloading")

        response = requests.get(jar_url, stream=True)
        if response.status_code != 200:
            self.logger.error(
                f"Download: Status Code {response.status_code} from {jar_url}"
            )
            return False

        with open(file, "wb") as f:
            shutil.copyfileobj(response.raw, f)

        self.logger.debug(f"Download: {len(response.content)} bytes received")

        if old_file and old_file != file and os.path.exists(old_file):
            os.remove(old_file)
            self.logger.info(f"Download complete: Updated {old_file} to {file}")
        else:
            self.logger.info(f"Download complete: Saved to {file}")

        return True


class SeedcrackerXExtractor(ModExtractor):
    """
    Extracts SeedcrackerX mod from the github repo.

    TODO: use the github api instead of scraping the readme
    """

    def __init__(
        self,
        version: str,
        source: str,
        file: str = None,
        name: str = "SeedcrackerX",
    ):
        super().__init__(name, version, source, file)

    def extract_jar(self) -> dict[str, str]:
        repo = self.source.split("github.com/")[1]
        readme = f"https://raw.githubusercontent.com/{repo}/master/README.md"
        releases = self.source + "/releases"

        # download readme file from the github url
        response = requests.get(readme)
        if response.status_code != 200:
            self.logger.error(
                f"Download: Status code {response.status_code} from {readme}"
            )
            return

        # extract the Version Tab table
        version_tab = re.search(
            r"#+\s+Version Tab\n(.*?)\n#+\s+", response.text, re.DOTALL
        )
        if not version_tab:
            self.logger.error(f"Version Tab not found in README.md")
            return

        mod_list = {
            mc_version: jar_url
            for mc_version, mod_version, jar_url in re.findall(
                r"\| +(.+?) +\| +\[(.+?)\]\((.+?)\)", version_tab.group(1)
            )
        }

        # try to find the exact VERSION a.b.c then a.b.x then a.b
        if self.version in mod_list:
            self.logger.debug(f"Exact version found: {self.version}")
            return {"version": self.version, "url": mod_list[self.version]}

        a, b, c = self.version.split(".")
        versions = [
            version for version in mod_list if re.search(rf"(^|\D){a}\.{b}\.", version)
        ]
        if len(versions) > 1:
            self.logger.error(f"More than one version found: {versions}")
            return

        if versions:
            self.logger.warning(f"No exact version found. Using: {versions[0]}")
            return {"version": versions[0], "url": mod_list[versions[0]]}

        if f"{a}.{b}" in mod_list:
            self.logger.warning(f"No exact version found. Using: {a}.{b}")
            return {"version": f"{a}.{b}", "url": mod_list[f"{a}.{b}"]}

        self.logger.error(f"No matching version found")
        return


if __name__ == "__main__":
    main()
