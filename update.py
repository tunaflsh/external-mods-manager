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

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "tunaflsh/external-mods-manager"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-d",
        "--debug",
        nargs="?",
        const="",
        default=[],
        action="append",
        help="Enable debug logging. Optionally specify a module (shown in magenta) to only enable debug logging for that module (e.g. -d Nyan-Work/itemscroller)",
    )
    args = parser.parse_args()

    logging.config.dictConfig(logging_config(args.debug))

    logger = logging.getLogger()

    modlist = json.load(open(MODSJSON, "r"))
    version = modlist["version"]

    disabled_mods = [mod for mod in modlist["mods"] if mod.get("disabled")]
    removing_mods = [mod for mod in disabled_mods if mod.get("file")]
    enabled_mods = [mod for mod in modlist["mods"] if not mod.get("disabled")]

    logger.info(
        "Updating %s for Minecraft %s",
        f"{len(enabled_mods)} mods" if len(enabled_mods) > 1 else "1 mod",
        version,
    )

    if removing_mods:
        logger.info(
            "Removing %s",
            f"{len(removing_mods)} mods" if len(removing_mods) > 1 else "1 mod",
        )
        for mod in removing_mods:
            if os.path.exists(mod["file"]):
                os.remove(mod["file"])
            del mod["file"]

    extractors = [
        extractor_factory(mod["name"], version, mod["source"], mod.get("file"))
        for mod in enabled_mods
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

    modlist["mods"] = list(mods.values()) + disabled_mods
    json.dump(modlist, open(MODSJSON, "w"), indent=4)

    logger.info("Done!")


def extractor_factory(
    name: str, version: str, source: str, file: str = None
) -> "ModExtractor":
    if name == "SeedcrackerX":
        return SeedcrackerXExtractor(version, source, file)
    else:
        return GithubReleasesExtractor(name, version, source, file)


class ModExtractor:
    GITHUB_REGEX = re.compile(
        r"^(https?://)?github.com/(?P<owner>.+?)/(?P<repo>.+?)(\.git)?$"
    )

    def __init__(self, name: str, version: str, source: str, file: str = None):
        self.name = name
        self.version = version
        self.source = source
        self.file = file
        self.logger = logging.getLogger(self.name)

    def __repr__(self) -> str:
        return self.name

    def extract_jars(self, response: requests.Response) -> dict[str, str]:
        return None

    def find_matching_version(self, mod_list: dict[str, str]) -> str:
        # try to find the exact VERSION a.b.c then a.b.* then a.b
        versions = [
            version
            for version in mod_list
            if re.search(rf"(^|\D){self.version}($|\D)$", version)
        ]
        if len(versions) > 1:
            self.logger.error(f"More than one version found: {versions}")
            return None

        if versions:
            self.logger.debug(f"Exact version found: {versions[0]}")
            return versions[0]

        a, b, c = self.version.split(".")
        versions = [
            version for version in mod_list if re.search(rf"(^|\D){a}\.{b}\.", version)
        ]
        if len(versions) > 1:
            self.logger.error(f"More than one version found: {versions}")
            return None

        if versions:
            self.logger.warning(f"No exact version found. Using: {versions[0]}")
            return versions[0]

        if f"{a}.{b}" in mod_list:
            self.logger.warning(f"No exact version found. Using: {a}.{b}")
            return f"{a}.{b}"

        self.logger.error(f"No matching version found")
        return None

    def download_jar(self) -> bool:
        """
        Returns:
            bool: True if the file was downloaded or up-to-date
        """
        jars = self.extract_jars()
        if not jars:
            self.logger.error(f"Failed to extract jars")
            return False

        version = self.find_matching_version(jars)
        if not version:
            self.logger.error(f"Failed to find matching version")
            return False

        self.version = version
        jar_url = jars[self.version]

        response = SESSION.head(jar_url, allow_redirects=True)
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
            return True

        self.logger.info(f"Downloading")

        response = SESSION.get(jar_url, stream=True)
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

    def extract_jars(self) -> dict[str, str]:
        owner, repo = self.GITHUB_REGEX.match(self.source).group("owner", "repo")
        readme = f"https://raw.githubusercontent.com/{owner}/{repo}/master/README.md"

        # download readme file from the github url
        response = SESSION.get(readme)
        if response.status_code != 200:
            self.logger.error(
                f"Download: Status code {response.status_code} from {readme}"
            )
            return None

        # extract the Version Tab table
        version_tab = re.search(
            r"#+\s+Version Tab\n(.*?)\n#+\s+", response.text, re.DOTALL
        )
        if not version_tab:
            self.logger.error(f"Version Tab not found in README.md")
            return None

        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"version_tab:\n{version_tab[1]}")

        mod_list = {
            match["mc_version"]: match["jar_url"]
            for match in re.finditer(
                r"\| +(?P<mc_version>[a-z0-9._-]+?\D?) +\| +\[(?P<mod_version>[0-9.]+?)\]\((?P<jar_url>\S+?)\) +\|",
                version_tab[1],
            )
        }

        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"mod_list:\n{json.dumps(mod_list, indent=4)}")

        return mod_list


class GithubReleasesExtractor(ModExtractor):
    """
    Extracts Nyan Work mods from the github repo.
    """

    VERSION_REGEX = re.compile(
        r"^(?P<mod_name>.+?)-(?P<game_version>\d+\.\d+(?:\.\d+)?(?:-pre\w+|-rc\w+)?|\d{2}w\d{2}\w+)-(?P<mod_version>.+?)\.jar$"
    )

    def __init__(self, name: str, version: str, source: str, file: str = None):
        super().__init__(name, version, source, file)

    def extract_jars(self) -> dict[str, str]:
        owner, repo = self.GITHUB_REGEX.match(self.source).group("owner", "repo")
        releases_url = f"https://api.github.com/repos/{owner}/{repo}/releases"

        # download releases page from the github url
        response = SESSION.get(releases_url)
        if response.status_code != 200:
            self.logger.error(
                f"Download: Status code {response.status_code} from {releases_url}"
            )
            return None

        releases = response.json()
        if not releases:
            self.logger.error(f"No releases found")
            return None

        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f"mod_list:\n{json.dumps({asset['name']: asset['browser_download_url'] for release in releases for asset in release['assets']}, indent=4)}"
            )

        mod_list = {
            self.VERSION_REGEX.match(asset["name"])["game_version"]: asset[
                "browser_download_url"
            ]
            for release in releases
            for asset in release["assets"]
        }

        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"mod_list:\n{json.dumps(mod_list, indent=4)}")

        return mod_list


if __name__ == "__main__":
    main()
