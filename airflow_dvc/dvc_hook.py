"""
High-level DVC client for building aced workflows.

@Piotr Styczyński 2021
"""
import datetime
import io
import os
import shutil
import tempfile
import pathlib
import time
from dataclasses import dataclass
from typing import Any, List, Optional, TextIO, Tuple

from airflow.hooks.base import BaseHook
from airflow.models.dag import DAG
from airflow.models.dagbag import DagBag
from git import Repo, exc

from airflow_dvc.dvc_cli import DVCLocalCli
from airflow_dvc.dvc_download import DVCDownload
from airflow_dvc.dvc_upload import DVCUpload
from airflow_dvc.exceptions import DVCFileMissingError, DVCGitRepoNotAccessibleError, DVCGitUpdateError
from airflow_dvc.logs import LOGS
from airflow_dvc.stats import DVCUpdateMetadata, DVCDownloadMetadata

try:
    # flake8: noqa
    from StringIO import StringIO  # # for Python 2
except ImportError:
    # flake8: noqa
    from io import StringIO  # # for Python 3


@dataclass
class DVCCommit:
    """
    Information about the commit created by the DVC operators
    """

    dvc_repo: str  # DVC repo URL
    dvc_repo_name: str  # Same as above
    message: str  # Commit message
    date: datetime.datetime  # Commit time
    dag: DAG  # DAG that triggered this commit
    files: List[str]  # List of modified files
    sha: str  # Commit sha
    commit_url: str  # Commit URL


@dataclass
class ClonedRepo:
    """
    Collection of information about the cloned repository.
    """

    clone_path: str  # Path to the clone directory
    temp_dir: tempfile.TemporaryDirectory  # Temporary directory object pointing to the clone path
    repo: Repo  # Git repo handler
    dvc: DVCLocalCli  # DVC interface


def clone_repo(
    dvc_repo: str,
    temp_path: Optional[str] = None,
):
    """
    Clone GIT repo configured for DVC.
    :param dvc_repo: Repository clone URL
    :param temp_path: Optional temporary path to store fetched data
    :returns: Cloned repo access object
    """
    if temp_path is None:
        temp_dir = tempfile.TemporaryDirectory()
    else:
        temp_dir = tempfile.TemporaryDirectory(dir=temp_path)
    clone_path = os.path.join(temp_dir.name, "repo")
    if os.path.isdir(clone_path):
        shutil.rmtree(clone_path)
    os.makedirs(clone_path, exist_ok=True)
    try:
        repo = Repo.clone_from(dvc_repo, clone_path)
    except exc.GitError as e:
        raise DVCGitRepoNotAccessibleError(dvc_repo, e)
    dvc = DVCLocalCli(clone_path)
    return clone_path, temp_dir, repo, dvc


try:
    import dvc.api as dvc_api
    import dvc.exceptions

    def dvc_open(repo: str, path: str, empty_fallback: bool = False) -> TextIO:
        """
        Open descriptor to the DVC file

        :param repo: Repo URL
        :param path: DVC file path
        :param empty_fallback: Create empty file when it does not exists remotely
          Otherwise function throws airflow_dvc.exceptions.DVCFileMissingError
        :returns: Descriptor to the file contents
        """
        try:
            # Url will fail if the file is missing
            dvc.api.get_url(
                path,
                repo=repo,
            )
            return dvc_api.open(
                path,
                repo=repo,
            )
        except dvc.exceptions.FileMissingError:
            if empty_fallback:
                return io.StringIO()
            raise DVCFileMissingError(repo, path)
        except dvc.exceptions.PathMissingError:
            if empty_fallback:
                return io.StringIO()
            raise DVCFileMissingError(repo, path)


except ModuleNotFoundError:
    # Fallback when DVC api module is not available
    # In this case we use DVC client
    def dvc_open(repo: str, path: str, empty_fallback: bool = False) -> TextIO:
        """
        Open descriptor to the DVC file

        :param repo: Repo URL
        :param path: DVC file path
        :param empty_fallback: Create empty file when it does not exists remotely
          Otherwise function throws airflow_dvc.exceptions.DVCFileMissingError
        :returns: Descriptor to the file contents
        """
        repo_url = repo
        clone_path, temp_dir, repo, dvc = clone_repo(repo)
        if not os.path.isfile(os.path.join(clone_path, f"{path}.dvc")):
            if empty_fallback:
                return io.StringIO()
            raise DVCFileMissingError(repo_url, path)
        # Pull the file
        dvc.pull_path(path)
        if not os.path.isfile(os.path.join(clone_path, path)):
            if empty_fallback:
                return io.StringIO()
            raise DVCFileMissingError(repo_url, path)
        with open(os.path.join(clone_path, path), "r") as dvc_file:
            input_stream = io.StringIO(dvc_file.read())
        temp_dir.cleanup()
        return input_stream


def repo_add_dvc_files(repo: Repo, files: List[str]):
    """
    Add DVC metadata files corresponding to the given ones to the GIT stage.
    :param repo: GIT repository object
    :param files: List of file paths
    """
    dvc_files = [f"{file}.dvc" for file in files]
    for dvc_file in dvc_files:
        repo.index.add([dvc_file])


class DVCFile:
    """
    Information about exisitng DVC file object
    """

    path: str  # File path
    dvc_repo: str  # Clone URL for the GIT repository that have DVC configured
    descriptor = None  # File-like object used for access
    empty_fallback: bool # Use empty file as fallback when it does not exists remotely

    def __init__(
        self,
        path: str,
        dvc_repo: str,
        empty_fallback: bool = False,
    ):
        """
        Create DVC file access object
        :param path: DVC file path
        :param dvc_repo: Clone URL for the GIT repo that have DVC configured
        """
        self.path = path
        self.dvc_repo = dvc_repo
        self.empty_fallback = empty_fallback
        self.descriptor = None

    def __enter__(self):
        """
        Open file for read and return file-like accessor
        """
        if self.descriptor is None:
            self.descriptor = dvc_open(
                self.dvc_repo,
                self.path,
                empty_fallback=self.empty_fallback,
            )
        return self.descriptor.__enter__()

    def __exit__(self, type, value, traceback):
        """
        Close file (close file-like accessor returned by __enter__)
        """
        if self.descriptor is not None:
            self.descriptor.__exit__(type, value, traceback)
        self.descriptor = None


class DVCHook(BaseHook):
    """
    Interface for all high-level DVC operations.
    For low-level DVC operations please see DVCLocalCli class.
    """

    dvc_repo: str

    def __init__(
        self,
        dvc_repo: str,
    ):
        """
        :param dvc_repo: Clone URL for the GIT repo that has DVC configured
        """
        super().__init__()
        self.dvc_repo = dvc_repo

    def get_conn(self) -> Any:
        return self

    def list_dag_commits(
        self,
        temp_path: Optional[str] = None,
    ) -> List[DVCCommit]:
        """
        Returns list of all commits generated for the given DVC repository.

        :param temp_path: Optional temporary clone path
        :returns: List with commits generated by the DVC operators
        """
        _, temp_dir, repo, _ = clone_repo(self.dvc_repo, temp_path)
        commits = list(
            repo.iter_commits(
                max_count=100,
            )
        )

        results: List[DVCCommit] = []
        for commit in commits:
            message_footer = commit.message.split("\n")[-1].split(" ")
            if len(message_footer) == 2 and message_footer[0] == "dag:":
                # Find DAGs responsible for this commit
                commit_dags = [
                    dag
                    for dag in DagBag().dags.values()
                    if dag.dag_id == message_footer[1]
                ]
                if len(commit_dags) > 0:
                    results.append(
                        DVCCommit(
                            dvc_repo=self.dvc_repo,
                            dvc_repo_name=self.dvc_repo,
                            files=[
                                file_path.replace(".dvc", "")
                                for file_path in commit.stats.files.keys()
                                if ".dvc" in file_path
                            ],
                            message="\n".join(commit.message.split("\n")[:-1]),
                            date=datetime.datetime.fromtimestamp(
                                commit.committed_date
                            ),
                            dag=commit_dags[0],
                            sha=commit.hexsha,
                            commit_url=f"{self.dvc_repo}/commits/{commit.hexsha}".replace(
                                ".git", ""
                            ),
                        )
                    )
        return results

    def modified_date(
        self,
        paths: List[str],
        temp_path: Optional[str] = None,
    ) -> datetime.datetime:
        """
        Get latest modification time for a given DVC-tracked file.
        The function will find the latest commit involving change to the DVC metadata
        file tracked in the repo that corresponds to the given input path.
        For multiple paths the max(...) of last modification dates is taken.

        :param paths: Paths to query the last modification date (max of dates will be taken)
        :param temp_path: Optional temporary clone path
        :returns: Time of last modification of the given files
        """
        _, temp_dir, repo, _ = clone_repo(self.dvc_repo, temp_path)
        commits = list(
            repo.iter_commits(
                max_count=100,
                paths=[f"{file_path}.dvc" for file_path in paths],
            )
        )
        last_modification_date = datetime.datetime.fromtimestamp(
            commits[0].committed_date
        )
        temp_dir.cleanup()
        return last_modification_date

    def download(
        self,
        downloaded_files: List[DVCDownload],
        empty_fallback: bool = False,
    ) -> DVCDownloadMetadata:
        """
        Download files from the DVC.
        For single-file access please see get(...) method.

        :param downloaded_files: Files to be downloaded
          (for more details see DVCDownload class)
        :param empty_fallback: Create empty file if it does not exists remotely
        """
        start = time.time()
        if len(downloaded_files) == 0:
            return DVCDownloadMetadata(
                dvc_repo=self.dvc_repo,
                downloaded_dvc_files=[],
                downloaded_dvc_files_sizes=[],
                duration=time.time()-start,
            )

        file_stats: List[Tuple[str, int]] = []
        for downloaded_file in downloaded_files:
            with self.get(downloaded_file.dvc_path, empty_fallback=empty_fallback) as data_input:
                content = data_input.read()
                downloaded_file.write(content)
                file_stats.append((downloaded_file.dvc_path, len(content)))

        return DVCDownloadMetadata(
            dvc_repo=self.dvc_repo,
            downloaded_dvc_files=[dvc_path for (dvc_path, _) in file_stats],
            downloaded_dvc_files_sizes=[file_size for (_, file_size) in file_stats],
            duration=time.time() - start,
        )

    def update(
        self,
        updated_files: List[DVCUpload],
        dag_id: str,
        commit_message: Optional[str] = None,
        temp_path: Optional[str] = None,
    ) -> DVCUpdateMetadata:
        """
        Update given files and upload them to DVC repo.
        The procedure involves pushing DVC changes and commiting
        changed metadata files back to the GIT repo.

        By default the commit message is as follows:
           DVC Automatically updated files: <list of files specified>

        :param updated_files: List of files to be uploaded as DVCUpload objects
          (please see DVCUpload class for more details)
        :param dag_id: DAG ID
        :param commit_message: Optional GIT commit message
        :param temp_path: Optional temporary clone path
        """
        start = time.time()
        if len(updated_files) == 0:
            return DVCUpdateMetadata(
                dvc_repo=self.dvc_repo,
                dag_id=dag_id,
                temp_path=temp_path,
                commit_message=None,
                dvc_files_updated=[],
                dvc_files_update_requested=[file.dvc_path for file in updated_files],
                commit_hexsha=None,
                committed_date=None,
                duration=time.time() - start,
            )

        if commit_message is None:
            file_list_str = ", ".join(
                [os.path.basename(file.dvc_path) for file in updated_files]
            )
            commit_message = (
                f"DVC Automatically updated files: {file_list_str}"
            )
        commit_message = f"{commit_message}\ndag: {dag_id}"

        LOGS.dvc_hook.info("Add files to DVC")
        clone_path, temp_dir, repo, dvc = clone_repo(self.dvc_repo, temp_path)
        for file in updated_files:
            with file as input_file:
                output_dvc_path = os.path.join(clone_path, file.dvc_path)
                pathlib.Path(os.path.dirname(os.path.abspath(output_dvc_path))).mkdir(parents=True, exist_ok=True)
                with open(output_dvc_path, "w") as out:
                    out.write(input_file.read())
            dvc.add(file.dvc_path)

        LOGS.dvc_hook.info("Push DVC")
        dvc.push()

        try:
            LOGS.dvc_hook.info("Add DVC files to git")
            repo_add_dvc_files(repo, [file.dvc_path for file in updated_files])

            LOGS.dvc_hook.info("Commit")
            commit = repo.index.commit(commit_message)

            LOGS.dvc_hook.info("Git push")
            repo.remotes.origin.push()
        except exc.GitError as e:
            raise DVCGitUpdateError(self.dvc_repo, [file.dvc_path for file in updated_files], e)

        meta = DVCUpdateMetadata(
            dvc_repo=self.dvc_repo,
            dag_id=dag_id,
            temp_path=temp_dir.name,
            commit_message=commit_message,
            dvc_files_updated=[file.dvc_path for file in updated_files],
            dvc_files_update_requested=[file.dvc_path for file in updated_files],
            commit_hexsha=commit.hexsha,
            committed_date=commit.committed_date,
            duration=time.time() - start,
        )
        LOGS.dvc_hook.info("Perform cleanup")
        temp_dir.cleanup()
        return meta

    def get(self, path: str, empty_fallback: bool = False) -> DVCFile:
        """
        Returns existing DVC file handler.
        This is useful to read the files.

        :param path: Path inside the DVC repo to the file you want to access
        :param empty_fallback: Create empty file when it does not exists remotely
          Otherwise with ... as ... will throw airflow_dvc.exceptions.DVCFileMissingError
        :returns: DVCFile handler corresponding to the given file
        """
        return DVCFile(
            path=path,
            dvc_repo=self.dvc_repo,
            empty_fallback=empty_fallback,
        )
