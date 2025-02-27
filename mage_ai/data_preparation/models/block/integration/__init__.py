import json
import os
import subprocess
from logging import Logger
from typing import Dict, List

import pandas as pd

from mage_ai.data_integrations.logger.utils import print_log_from_line
from mage_ai.data_integrations.utils.config import build_config, get_catalog_by_stream
from mage_ai.data_preparation.models.block import PYTHON_COMMAND, Block
from mage_ai.data_preparation.models.constants import BlockType
from mage_ai.shared.hash import merge_dict
from mage_ai.shared.security import filter_out_config_values


class IntegrationBlock(Block):
    def _execute_block(
        self,
        outputs_from_input_vars,
        execution_partition: str = None,
        from_notebook: bool = False,
        global_vars: Dict = None,
        input_vars: List = None,
        logger: Logger = None,
        logging_tags: Dict = None,
        input_from_output: Dict = None,
        runtime_arguments: Dict = None,
        **kwargs,
    ) -> List:
        from mage_integrations.sources.constants import BATCH_FETCH_LIMIT

        if logging_tags is None:
            logging_tags = dict()

        index = self.template_runtime_configuration.get('index', None)
        is_last_block_run = self.template_runtime_configuration.get('is_last_block_run', False)
        selected_streams = self.template_runtime_configuration.get('selected_streams', [])
        stream = selected_streams[0] if len(selected_streams) >= 1 else None
        destination_table = self.template_runtime_configuration.get('destination_table', stream)
        query_data = runtime_arguments or {}
        query_data = query_data.copy()

        tags = dict(block_tags=dict(
            destination_table=destination_table,
            index=index,
            stream=stream,
            type=self.type,
            uuid=self.uuid,
        ))
        updated_logging_tags = merge_dict(
            logging_tags,
            dict(tags=tags),
        )

        variables_dictionary_for_config = merge_dict(global_vars, {
            'pipeline.name': self.pipeline.name if self.pipeline else None,
            'pipeline.uuid': self.pipeline.uuid if self.pipeline else None,
        })

        if index is not None:
            source_state_file_path = self.pipeline.source_state_file_path(
                destination_table=destination_table,
                stream=stream,
            )
            destination_state_file_path = self.pipeline.destination_state_file_path(
                destination_table=destination_table,
                stream=stream,
            )
            source_output_file_path = self.pipeline.source_output_file_path(stream, index)

            stream_catalog = get_catalog_by_stream(
                self.pipeline.data_loader.file_path,
                stream,
                global_vars,
                pipeline=self.pipeline,
            ) or dict()

            if stream_catalog.get('replication_method') == 'INCREMENTAL':
                from mage_integrations.sources.utils import (
                    update_source_state_from_destination_state,
                )
                update_source_state_from_destination_state(
                    source_state_file_path,
                    destination_state_file_path,
                )
            else:
                query_data['_offset'] = BATCH_FETCH_LIMIT * index
            if not is_last_block_run:
                query_data['_limit'] = BATCH_FETCH_LIMIT

        outputs = []
        if BlockType.DATA_LOADER == self.type:
            lines_in_file = 0

            with open(source_output_file_path, 'w') as f:
                config, config_json = build_config(
                    self.pipeline.data_loader.file_path,
                    variables_dictionary_for_config,
                )
                args = [
                    PYTHON_COMMAND,
                    self.pipeline.source_file_path,
                    '--config_json',
                    config_json,
                    '--log_to_stdout',
                    '1',
                    '--settings',
                    self.pipeline.settings_file_path,
                    '--state',
                    source_state_file_path,
                    '--query_json',
                    json.dumps(query_data),
                ]

                if len(selected_streams) >= 1:
                    args += [
                        '--selected_streams_json',
                        json.dumps(selected_streams),
                    ]

                proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

                for line in proc.stdout:
                    f.write(filter_out_config_values(line.decode(), config)),
                    print_log_from_line(
                        line,
                        config=config,
                        logger=logger,
                        logging_tags=logging_tags,
                        tags=tags,
                    )
                    lines_in_file += 1

                outputs.append(proc)

            proc.communicate()
            if proc.returncode != 0 and proc.returncode is not None:
                cmd = proc.args if isinstance(proc.args, str) else str(proc.args)
                raise subprocess.CalledProcessError(
                    proc.returncode,
                    filter_out_config_values(cmd, config),
                )

            file_size = os.path.getsize(source_output_file_path)
            msg = f'Finished writing {file_size} bytes with {lines_in_file} lines to output '\
                  f'file {source_output_file_path}.'
            if logger:
                logger.info(msg, **updated_logging_tags)
            else:
                print(msg)
        elif BlockType.TRANSFORMER == self.type:
            from mage_integrations.sources.constants import COLUMN_TYPE_NULL
            from mage_integrations.transformers.utils import (
                convert_data_type,
                infer_dtypes,
            )
            from mage_integrations.utils.logger.constants import (
                TYPE_RECORD,
                TYPE_SCHEMA,
            )

            decorated_functions = []
            test_functions = []

            results = {
                self.type: self._block_decorator(decorated_functions),
                'test': self._block_decorator(test_functions),
            }
            results.update(outputs_from_input_vars)

            exec(self.content, results)

            # 1. Recreate each record
            # 2. Recreate schema
            schema_original = None
            schema_updated = None
            schema_index = None
            output_arr = []
            records_transformed = 0
            df_sample = None

            with open(source_output_file_path, 'r') as f:
                idx = 0
                for line in f:
                    line = line.strip() if line else ''
                    if len(line) == 0:
                        continue

                    try:
                        data = json.loads(line)
                        line_type = data.get('type')

                        if TYPE_SCHEMA == line_type:
                            schema_index = idx
                            schema_original = data
                        elif TYPE_RECORD == line_type:
                            record = data['record']
                            input_vars = [pd.DataFrame.from_dict([record])]
                            input_kwargs = merge_dict(
                                global_vars,
                                dict(
                                    index=index,
                                    query=query_data,
                                    stream=stream,
                                ),
                            )
                            block_function = self._validate_execution(
                                decorated_functions,
                                input_vars,
                            )

                            if block_function is not None:
                                df = self.execute_block_function(
                                    block_function,
                                    input_vars,
                                    global_vars=input_kwargs,
                                    from_notebook=from_notebook,
                                )
                                if df_sample is None:
                                    df_sample = df

                                if not schema_updated:
                                    properties_updated = {
                                        k: dict(type=[COLUMN_TYPE_NULL, convert_data_type(v)])
                                        for k, v in infer_dtypes(df).items()
                                    }
                                    schema_updated = schema_original.copy()
                                    properties_original = schema_updated['schema']['properties']
                                    schema_updated['schema']['properties'] = {
                                        k: properties_original[k]
                                        if k in properties_original else v
                                        for k, v in properties_updated.items()
                                    }

                                if df.shape[0] == 0:
                                    continue
                                record_transformed = df.to_dict('records')[0]

                                line = json.dumps(merge_dict(
                                    data,
                                    dict(record=record_transformed),
                                ))
                                records_transformed += 1

                                if records_transformed % 1000 == 0:
                                    msg = f'{records_transformed} records have been transformed...'
                                    if logger:
                                        logger.info(msg, **updated_logging_tags)
                                    else:
                                        print(msg)
                    except json.decoder.JSONDecodeError:
                        pass

                    output_arr.append(line)
                    idx += 1

            output_arr[schema_index] = json.dumps(schema_updated)

            with open(source_output_file_path, 'w') as f:
                output = '\n'.join(output_arr)
                f.write(output)

            msg = f'Transformed {records_transformed} total records for stream {stream}.'
            file_size = os.path.getsize(source_output_file_path)
            msg2 = f'Finished writing {file_size} bytes with {len(output_arr)} lines to '\
                   f'output file {source_output_file_path}.'
            if logger:
                logger.info(msg, **updated_logging_tags)
                logger.info(msg2, **updated_logging_tags)
            else:
                print(msg)
                print(msg2)

            self.test_functions = test_functions
        elif BlockType.DATA_EXPORTER == self.type:
            override = {}
            if destination_table:
                override['table'] = destination_table

            file_size = os.path.getsize(source_output_file_path)
            msg = f'Reading {file_size} bytes from {source_output_file_path} as input file.'
            if logger:
                logger.info(msg, **updated_logging_tags)
            else:
                print(msg)

            config, config_json = build_config(
                self.pipeline.data_exporter.file_path,
                variables_dictionary_for_config,
                override=override,
            )

            proc = subprocess.Popen([
                PYTHON_COMMAND,
                self.pipeline.destination_file_path,
                '--config_json',
                config_json,
                '--log_to_stdout',
                '1',
                '--settings',
                self.pipeline.data_exporter.file_path,
                '--state',
                self.pipeline.destination_state_file_path(
                    destination_table=destination_table,
                    stream=stream,
                ),
                '--input_file_path',
                source_output_file_path,
            ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            for line in proc.stdout:
                print_log_from_line(
                    line,
                    config=config,
                    logger=logger,
                    logging_tags=logging_tags,
                    tags=tags,
                )

            proc.communicate()
            if proc.returncode != 0 and proc.returncode is not None:
                cmd = proc.args if isinstance(proc.args, str) else str(proc.args)
                raise subprocess.CalledProcessError(
                    proc.returncode,
                    filter_out_config_values(cmd, config),
                )

            outputs.append(proc)

        return outputs


class SourceBlock(IntegrationBlock):
    pass


class DestinationBlock(IntegrationBlock):
    def to_dict(
        self,
        include_content=False,
        include_outputs=False,
        sample_count=None,
        check_if_file_exists: bool = False,
        destination_table: str = None,
        state_stream: str = None,
    ):
        data = {}
        if state_stream and destination_table:
            from mage_ai.data_preparation.models.pipelines.integration_pipeline import (
                IntegrationPipeline,
            )
            integration_pipeline = IntegrationPipeline(self.pipeline.uuid)
            destination_state_file_path = integration_pipeline.destination_state_file_path(
                destination_table=destination_table,
                stream=state_stream,
            )
            if os.path.isfile(destination_state_file_path):
                with open(destination_state_file_path, 'r') as f:
                    text = f.read()
                    d = json.loads(text) if text else {}
                    bookmark_values = d.get('bookmarks', {}).get(state_stream)
                    data['bookmarks'] = bookmark_values

        return merge_dict(
            super().to_dict(
                include_content,
                include_outputs,
                sample_count,
                check_if_file_exists,
            ),
            data,
        )

    def update(self, data, update_state=False):
        if update_state:
            from mage_ai.data_preparation.models.pipelines.integration_pipeline import (
                IntegrationPipeline,
            )
            from mage_integrations.destinations.utils import (
                update_destination_state_bookmarks,
            )

            integration_pipeline = IntegrationPipeline(self.pipeline.uuid)
            tap_stream_id = data.get('tap_stream_id')
            destination_table = data.get('destination_table')
            bookmark_values = data.get('bookmark_values', {})
            if tap_stream_id and destination_table:
                destination_state_file_path = integration_pipeline.destination_state_file_path(
                    destination_table=destination_table,
                    stream=tap_stream_id,
                )
                update_destination_state_bookmarks(
                    destination_state_file_path,
                    tap_stream_id,
                    bookmark_values=bookmark_values
                )

        return super().update(data)

    def output_variables(self, execution_partition: str = None) -> List[str]:
        return []


class TransformerBlock(IntegrationBlock):
    pass
