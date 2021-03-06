"""Utilities for working with KNIME workflows and data.

TODOs:
    * add tool to view comments on container input nodes to help id them
    * add tests which use knime workflow directory checked into test dir
    * make sure setup.py doesn't install stuff from test/
    * expose/list on PyPI properly
    * add handling for setting of workflow variables
      - only via Container Input (Variable) nodes?
      - also via batch executor variable settings?
    * add handling for Container Input/Output (JSON) nodes

"""


import json
import xml.etree.ElementTree as ElementTree
from pathlib import Path, PurePosixPath
import tempfile
import subprocess
import shlex
import logging
import os


__author__ = "Appliomics, LLC"
__copyright__ = "Copyright 2018, KNIME.com AG"
__credits__ = [ "Davin Potts", "Greg Landrum" ]
__license__ = "???"
__version__ = "0.8.1"


__all__ = [ "Workflow", "LocalWorkflow", "RemoteWorkflow", "executable_path" ]


if os.name == "nt":
    executable_path = r"C:\Program Files\KNIME\knime.exe"
else:
    executable_path = "/opt/local/knime_3.6.0/knime"


def find_service_table_node_dirnames(path_to_knime_workflow):
    """Returns a tuple containing the unique directory names of the Container
    Input and Output (Table) nodes employed by the KNIME workflow in the
    specified path on disk.  The output tuple contains two lists, the first
    lists Container Input (Table) node directory names and the second lists
    Container Output (Table) nodes."""

    input_service_table_node_dirnames = []
    output_service_table_node_dirnames = []

    for settings_filepath in Path(path_to_knime_workflow).glob("*/settings.xml"):
        with settings_filepath.open() as fh:
            for line in fh:
                if "ContainerTableInputNodeFactory" in line:
                    *extra, dirname, _settings_xml = settings_filepath.parts
                    input_service_table_node_dirnames.append(dirname)
                    break
                elif "ContainerTableOutputNodeFactory" in line:
                    *extra, dirname, _settings_xml = settings_filepath.parts
                    output_service_table_node_dirnames.append(dirname)
                    break

    return input_service_table_node_dirnames, output_service_table_node_dirnames


def find_node_id(path_to_knime_workflow, unique_node_dirname):
    """Returns the unique node id for a KNIME node identified by its
    unique directory name on disk.  For example, a Container Input (Table)
    Node appearing in a KNIME workflow is given a unique directory name
    on disk such as "Container Input _Table_ (#42)"."""

    tree = ElementTree.parse(Path(path_to_knime_workflow, "workflow.knime"))
    top_config = tree.getroot()

    for entry in top_config:
        if entry.attrib.get("key") == "nodes" and entry.tag.endswith("config"):
            # Attempt to infer the namespace being used rather than require
            # one particular version of the KNIME XML namespace.
            config_tag_name = entry.tag
            break
    else:
        raise IndexError("nodes config XML tag not found")

    target_value = str(PurePosixPath(unique_node_dirname, "settings.xml"))
    for node_config in entry.iterfind(config_tag_name):
        for sub_tag in node_config:
            if sub_tag.attrib.get("key") == "id":
                node_id = int(sub_tag.attrib["value"])
            if sub_tag.attrib.get("value") == target_value:
                found_service_table = True
                break
        else:
            node_id = None
        if node_id is not None:
            break

    return node_id


map_numpy_to_knime_type = (
    ('float', 'double'),
    ('int64', 'long'),
    ('long', 'long'),
    ('int', 'int'),
    ('bool', 'boolean')
)

def pandas_type_mapper(pandas_dtype):
    "Converts a pandas dtype to a comparable KNIME data type (as a string)."
    key = str(pandas_dtype)
    for np_type, knime_type in map_numpy_to_knime_type:
        if np_type in key:
            return knime_type
    return 'string'


def convert_dataframe_to_knime_friendly_dict(df):
    """Produces a dict from a pandas DataFrame-like input that is structured
    to be friendly to KNIME when converted to then consumed as json."""

    proto_table_spec = [
        (column_name, pandas_type_mapper(dtype))
        for column_name, dtype in df.dtypes.items()
    ]

    # If an encountered column's dtype does not readily map to a KNIME
    # data type, it will be conveyed to KNIME as a 'string'.  To ensure
    # proper conversion to json, a copy of the original DataFrame is
    # created, containing str values in otherwise problematic columns.
    df2 = df.copy()
    for column_name, knime_type in proto_table_spec:
        if knime_type == "string":
            df2[column_name] = df2[column_name].apply(str)

    data = { 
        "table-spec": [ {c: t} for c, t in proto_table_spec ],
        "table-data": df2.to_dict(orient="split")["data"],
    }

    return data


def run_workflow_using_multiple_service_tables(
        input_datas,
        path_to_knime_executable,
        path_to_knime_workflow,
        input_service_table_node_ids,
        output_service_table_node_ids,
        *,
        live_passthru_stdout_stderr=False,
        output_as_pandas_dataframes=True,
        input_json_filename_pattern="input_%d.json",
        output_json_filename_pattern="output_%d.json",
    ):
    """Executes the requested KNIME workflow, feeding the supplied data
    to the Container Input (Table) nodes in that workflow and returning the
    output from the workflow's Container Output (Table) nodes."""

    abspath_to_knime_workflow = Path(path_to_knime_workflow).absolute()
    if not Path(path_to_knime_executable).exists():
        raise ValueError(f"Executable not found: {path_to_knime_executable}")

    with tempfile.TemporaryDirectory() as temp_dir:
        logging.debug(f"using temp dir: {temp_dir}")

        option_flags_input_service_table_nodes = []
        for node_id, data in zip(input_service_table_node_ids, input_datas):
            input_json_filename = input_json_filename_pattern % node_id
            input_json_filepath = Path(temp_dir, input_json_filename)

            # Support pandas DataFrame-like inputs.
            try:
                data = convert_dataframe_to_knime_friendly_dict(data)
            except AttributeError:
                pass

            with open(input_json_filepath, "w") as input_json_fh:
                json.dump(data, input_json_fh)

            option_flags_input_service_table_nodes.append(
                f'-option={node_id},inputPathOrUrl,"{input_json_filepath}",String'
            )

        option_flags_output_service_table_nodes = []
        expected_output_json_files = []
        for node_id in output_service_table_node_ids:
            output_json_filename = output_json_filename_pattern % node_id
            output_json_filepath = Path(temp_dir, output_json_filename)

            option_flags_input_service_table_nodes.append(
                f'-option={node_id},outputPathOrUrl,"{output_json_filepath}",String',
            )
            expected_output_json_files.append(output_json_filepath)

        data_dir = Path(temp_dir, "knime_data")

        # shlex.quote handles executable paths containing spaces, etc.
        # On Windows, cmd shell requires double-quotes, hence replace()
        shell_command = " ".join([
            shlex.quote(path_to_knime_executable).replace("'", '"'),
            "-nosplash",
            "-debug",
            "--launcher.suppressErrors",
            "-application org.knime.product.KNIME_BATCH_APPLICATION",
            f"-data {data_dir}",
            f'-workflowDir="{abspath_to_knime_workflow}"',
            " ".join(option_flags_input_service_table_nodes),
            " ".join(option_flags_output_service_table_nodes),
        ])
        logging.info(f"knime invocation: {shell_command}")

        result = subprocess.run(
            shell_command,
            shell=True,
            stdout=subprocess.PIPE if not live_passthru_stdout_stderr else None,
            stderr=subprocess.PIPE if not live_passthru_stdout_stderr else None,
        )
        logging.info(f"exit code from KNIME execution: {result.returncode}")

        knime_outputs = []
        try:
            for output_json_filepath in expected_output_json_files:
                with open(output_json_filepath) as output_json_fh:
                    single_node_knime_output = json.load(output_json_fh)
                knime_outputs.append(single_node_knime_output)
        except FileNotFoundError:
            logging.error(f"captured stdout: {result.stdout}")
            logging.error(f"captured stderr: {result.stderr}")
            raise ChildProcessError("Output from KNIME not found")

        if output_as_pandas_dataframes:
            try:
                import pandas as pd
                for i, output in enumerate(knime_outputs):
                    df_columns = list(
                        k for d in output['table-spec']
                        for k, v in d.items()
                    )
                    knime_outputs[i] = pd.DataFrame(
                        output['table-data'],
                        columns=df_columns
                    )
            except ImportError:
                logging.warning("requested output as DataFrame not possible")
            except Exception as e:
                logging.error("error while converting KNIME output to DataFrame")
                raise e

        if result.returncode != 0:
            logging.info(f"captured stdout: {result.stdout}")
            logging.info(f"captured stderr: {result.stderr}")

    return knime_outputs


class Workflow:
    "Factory class for working with KNIME workflows; not for subclassing."

    def __new__(cls, workflow_path_or_url):
        if workflow_path_or_url.startswith(r"knime://"):
            # URL for workflow on KNIME Server is handled by RemoteWorkflow
            cls = RemoteWorkflow
        else:
            # Local filesystem workflow is handled by LocalWorkflow
            cls = LocalWorkflow
        return cls(workflow_path_or_url)


class LocalWorkflow:
    "Tools for reading and executing local KNIME workflows."

    __slots__ = ("_data_table_inputs", "_data_table_outputs",
            "_service_table_input_nodes", "_service_table_output_nodes",
            "path_to_knime_workflow", "_input_ids", "_output_ids")

    def __init__(self, workflow_path):
        self.path_to_knime_workflow = Path(workflow_path).absolute()
        self._data_table_inputs = None
        self._data_table_outputs = None
        self._service_table_input_nodes = None
        self._service_table_output_nodes = None

    def __enter__(self):
        self._discover_inputoutput_nodes()
        return self

    def __dir__(self):
        return [ a for a in dir(self.__class__) if a[0] != "_" or a[1] == "_" ]

    def __exit__(self, exc_type, exc_inst, exc_tb):
        return False

    def _discover_inputoutput_nodes(self):
        self._service_table_input_nodes, self._service_table_output_nodes = \
            find_service_table_node_dirnames(self.path_to_knime_workflow)
        self._input_ids = [
            find_node_id(self.path_to_knime_workflow, stin)
            for stin in self._service_table_input_nodes
        ]
        self._output_ids = [
            find_node_id(self.path_to_knime_workflow, stin)
            for stin in self._service_table_output_nodes
        ]
        self._data_table_inputs = [None] * len(self._service_table_input_nodes)
        self._data_table_outputs = [None] * len(self._service_table_output_nodes)

    def execute(self, live_passthru_stdout_stderr=False):
        "Executes the KNIME workflow via KNIME's batch executor."
        outputs = run_workflow_using_multiple_service_tables(
            self.data_table_inputs,
            executable_path,
            self.path_to_knime_workflow,
            self._input_ids,
            self._output_ids,
            live_passthru_stdout_stderr=live_passthru_stdout_stderr,
        )
        self._data_table_outputs[:] = outputs

    @property
    def data_table_inputs(self):
        """List of inputs (data) to be supplied to the Container Input nodes
        in the KNIME workflow at time of execution.  Growing or shrinking this
        list from its original length is not supported.  This list is not
        guaranteed to persist after __exit__ is called."""
        if self._service_table_input_nodes is None or self._data_table_inputs is None:
            self._discover_inputoutput_nodes()
        return self._data_table_inputs

    @property
    def data_table_outputs(self):
        """List of outputs produced from Container Output nodes in the KNIME
        workflow (populated only after execution).  This list is not
        guaranteed to persist after __exit__ is called."""
        if self._service_table_output_nodes is None or self._data_table_outputs is None:
            self._discover_inputoutput_nodes()
        return self._data_table_outputs

    @property
    def data_table_inputs_names(self):
        "View of which Container Input nodes go with which position in list."
        return tuple(self._service_table_input_nodes)

    def _repr_svg_(self):
        "Displays SVG of workflow in Jupyter notebook."
        from IPython.display import SVG, display
        display(SVG(url=(self.path_to_knime_workflow / "workflow.svg").as_uri()))


class RemoteWorkflow(LocalWorkflow):
    "Tools for reading and executing remote KNIME workflows on a Server."

    def __init__(self, workflow_url_on_server):
        self.path_to_knime_workflow = workflow_url_on_server
        raise NotImplementedError("%s not yet implemented" % self.__class__.__name__)


