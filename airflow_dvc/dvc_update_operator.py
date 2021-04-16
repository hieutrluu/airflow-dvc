"""
Airflow operator to upload files to DVC.

@Piotr Styczyński 2021
"""
from typing import Optional, List, Tuple, Union
from airflow.models.python_operator import PythonOperator
from airflow.utils.decorators import apply_defaults
from collections.abc import Callable

from airflow_dvc.dvc_client import DVCClient
from airflow_dvc.dvc_upload import DVCUpload

Uploads = Union[List[DVCUpload], Callable[any, List[DVCUpload]]]


class DVCUpdateOperator(PythonOperator):
    """
    Operator that allows DAGs to update DVC files.
    You can use it to upload various types of sources.
    For more information please see DVCUpload abstract class.
    """

    dvc_repo: str # Clone URL for a GIT repo
    files: Uploads # List of files to be uploaded or function that returns it
    commit_message: Optional[str] # Optional Git custom commit message
    temp_path: Optional[str] # Path to a temporary clone directory

    @apply_defaults
    def __init__(
            self,
            dvc_repo: str,
            files: Uploads,
            commit_message: Optional[str] = None,
            temp_path: Optional[str] = None,
            **kwargs
        ) -> None:
        """
        Creates Airflow upload operator.

        :param dvc_repo: Git clone url for repo with configured DVC
        :param files: Files to be uploaded (please see DVCUpload class for more details)
        """
        super().__init__(**kwargs, python_callable=self._execute_operator)
        self.dvc_repo = dvc_repo
        self.files = files
        self.commit_message = commit_message
        self.temp_path = temp_path

    def _execute_operator(self, *args, **kwargs):
        """
        Perform the DVC uploads.
        """
        files = self.files
        if callable(self.files):
            files = self.files(*args, **kwargs)
        dvc = DVCClient(self.dvc_repo)
        dvc.update(
            updated_files=files,
            commit_message=self.commit_message,
            temp_path=self.temp_path,
        )