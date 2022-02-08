# Copyright (C) 2021 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause
# See: https://spdx.org/licenses/
from __future__ import annotations

import sys
import typing
import typing as ty

import numpy as np

from lava.magma.compiler.channels.pypychannel import CspSendPort, CspRecvPort
from lava.magma.compiler.exec_var import AbstractExecVar
from lava.magma.core.process.message_interface_enum import ActorType
from lava.magma.runtime.message_infrastructure.message_infrastructure_interface\
    import MessageInfrastructureInterface
from lava.magma.runtime.message_infrastructure.factory import \
    MessageInfrastructureFactory
from lava.magma.runtime.mgmt_token_enums import enum_to_np, enum_equal, \
    MGMT_COMMAND, MGMT_RESPONSE
from lava.magma.runtime.runtime_service import AsyncPyRuntimeService

if ty.TYPE_CHECKING:
    from lava.magma.core.process.process import AbstractProcess
from lava.magma.compiler.builder import AbstractProcessBuilder, \
    RuntimeChannelBuilderMp, ServiceChannelBuilderMp, \
    RuntimeServiceBuilder
from lava.magma.compiler.channels.interfaces import Channel
from lava.magma.core.resources import HeadNode
from lava.magma.core.run_conditions import RunSteps, RunContinuous
from lava.magma.compiler.executable import Executable
from lava.magma.compiler.node import NodeConfig
from lava.magma.core.run_conditions import AbstractRunCondition


# Function to build and attach a system process to
def target_fn(*args, **kwargs):
    builder = kwargs.pop("builder")
    actor = builder.build()
    actor.start(*args, **kwargs)


class Runtime:
    """Lava runtime which consumes an executable and add_ports_for_polling run_condition. Exposes
    the APIs to start, pause, stop and wait on an execution. Execution could
    be blocking and non-blocking as specified by the add_ports_for_polling run_condition."""

    def __init__(self,
                 exe: Executable,
                 message_infrastructure_type: ActorType):
        self._run_cond: typing.Optional[AbstractRunCondition] = None
        self._executable: Executable = exe

        self._messaging_infrastructure_type: ActorType = \
            message_infrastructure_type
        self._messaging_infrastructure: \
            ty.Optional[MessageInfrastructureInterface] = None
        self._is_initialized: bool = False
        self._is_running: bool = False
        self._is_started: bool = False
        self._req_paused: bool = False
        self._req_stop: bool = False
        self._error: bool = False
        self.runtime_to_service: ty.Iterable[CspSendPort] = []
        self.service_to_runtime: ty.Iterable[CspRecvPort] = []

    def __del__(self):
        """On destruction, terminate Runtime automatically to
        free compute resources.
        """
        if self._is_started:
            self.stop()

    def initialize(self):
        """Initializes the runtime"""
        # Right now assume there is only 1 node config
        node_configs: ty.List[NodeConfig] = self._executable.node_configs
        if len(node_configs) != 1:
            raise AssertionError

        node_config: NodeConfig = node_configs[0]

        # Right now assume there is only 1 node in node_config with resource
        # type CPU
        if len(node_config) != 1:
            raise AssertionError
        if node_config[0].node_type != HeadNode:
            raise AssertionError

        self._build_message_infrastructure()
        self._build_channels()
        self._build_sync_channels()
        self._build_processes()
        self._build_runtime_services()
        self._start_ports()
        self._is_initialized = True

    def _start_ports(self):
        for port in self.runtime_to_service:
            port.start()
        for port in self.service_to_runtime:
            port.start()

    # ToDo: (AW) Hack: This currently just returns the one and only NodeCfg
    @property
    def node_cfg(self) -> NodeConfig:
        """Returns the selected NodeCfg."""
        return self._executable.node_configs[0]

    def _build_message_infrastructure(self):
        self._messaging_infrastructure = MessageInfrastructureFactory.create(
            self._messaging_infrastructure_type)
        self._messaging_infrastructure.start()

    def _get_process_builder_for_process(self, process):
        process_builders: ty.Dict[
            "AbstractProcess", "AbstractProcessBuilder"
        ] = {}
        process_builders.update(self._executable.c_builders)
        process_builders.update(self._executable.py_builders)
        process_builders.update(self._executable.nc_builders)
        return process_builders[process]

    def _build_channels(self):
        if self._executable.channel_builders:
            for channel_builder in self._executable.channel_builders:
                channel = channel_builder.build(
                    self._messaging_infrastructure
                )
                self._get_process_builder_for_process(
                    channel_builder.src_process).set_csp_ports(
                    [channel.src_port])
                self._get_process_builder_for_process(
                    channel_builder.dst_process).set_csp_ports(
                    [channel.dst_port])

    def _build_sync_channels(self):
        if self._executable.sync_channel_builders:
            for sync_channel_builder in self._executable.sync_channel_builders:
                channel: Channel = sync_channel_builder.build(
                    self._messaging_infrastructure
                )
                if isinstance(sync_channel_builder, RuntimeChannelBuilderMp):
                    if isinstance(sync_channel_builder.src_process,
                                  RuntimeServiceBuilder):
                        sync_channel_builder.src_process.set_csp_ports(
                            [channel.src_port])
                    else:
                        sync_channel_builder.dst_process.set_csp_ports(
                            [channel.dst_port])
                    # TODO: Get rid of if/else ladder
                    if "runtime_to_service" in channel.src_port.name:
                        self.runtime_to_service.append(channel.src_port)
                    elif "service_to_runtime" in channel.src_port.name:
                        self.service_to_runtime.append(channel.dst_port)
                elif isinstance(sync_channel_builder, ServiceChannelBuilderMp):
                    if isinstance(sync_channel_builder.src_process,
                                  RuntimeServiceBuilder):
                        sync_channel_builder.src_process.set_csp_proc_ports(
                            [channel.src_port])
                        self._get_process_builder_for_process(
                            sync_channel_builder.dst_process).set_rs_csp_ports(
                            [channel.dst_port])
                    else:
                        sync_channel_builder.dst_process.set_csp_proc_ports(
                            [channel.dst_port])
                        self._get_process_builder_for_process(
                            sync_channel_builder.src_process).set_rs_csp_ports(
                            [channel.src_port])
                else:
                    raise ValueError("Unexpected type of Sync Channel Builder")

    # ToDo: (AW) Why not pass the builder as an argument to the mp.Process
    #  constructor which will then be passed to the target function?
    def _build_processes(self):
        process_builders_collection: ty.List[
            ty.Dict[AbstractProcess, AbstractProcessBuilder]] = [
            self._executable.py_builders,
            self._executable.c_builders,
            self._executable.nc_builders,
        ]

        for process_builders in process_builders_collection:
            if process_builders:
                for proc, proc_builder in process_builders.items():
                    # Assign current Runtime to process
                    proc._runtime = self
                    self._messaging_infrastructure.build_actor(
                        target_fn=target_fn,
                        builder=proc_builder)

    def _build_runtime_services(self):
        runtime_service_builders = self._executable.rs_builders
        if self._executable.rs_builders:
            for sd, rs_builder in runtime_service_builders.items():
                self._messaging_infrastructure.build_actor(
                    target_fn=target_fn,
                    builder=rs_builder)

    def start(self, run_condition: AbstractRunCondition):
        if self._is_initialized:
            # Start running
            self._is_started = True
            self._run(run_condition)
        else:
            print("Runtime not initialized yet.")

    def _get_resp_for_run(self):
        """
        Gets response from RuntimeServices
        """
        if self._is_running:
            for recv_port in self.service_to_runtime:
                data = recv_port.recv()
                if enum_equal(data, MGMT_RESPONSE.REQ_PAUSE):
                    self._req_paused = True
                elif enum_equal(data, MGMT_RESPONSE.REQ_STOP):
                    self._req_stop = True
                elif not enum_equal(data, MGMT_RESPONSE.DONE):
                    if enum_equal(data, MGMT_RESPONSE.ERROR):
                        # Receive all errors from the ProcessModels
                        error_cnt = 0
                        for actors in \
                                self._messaging_infrastructure.actors:
                            actors.join()
                            if actors.exception:
                                _, traceback = actors.exception
                                print(traceback)
                                error_cnt += 1
                        self._error = True
                    else:
                        raise RuntimeError(f"Runtime Received {data}")
            if self._req_paused:
                self._req_paused = False
                self.pause()
            if self._req_stop:
                self._req_stop = False
                self.stop()
            if self._error:
                # self.stop()
                raise RuntimeError(
                    f"{error_cnt} Exception(s) occurred. See "
                    f"output above for details.")
            self._is_running = False

    def _run(self, run_condition: AbstractRunCondition):
        if self._is_started:
            self._is_running = True
            if isinstance(run_condition, RunSteps):
                self.num_steps = run_condition.num_steps
                for send_port in self.runtime_to_service:
                    send_port.send(enum_to_np(self.num_steps))
                if run_condition.blocking:
                    self._get_resp_for_run()
            elif isinstance(run_condition, RunContinuous):
                self.num_steps = sys.maxsize
                for send_port in self.runtime_to_service:
                    send_port.send(enum_to_np(self.num_steps))
            else:
                raise ValueError(f"Wrong type of run_condition : "
                                 f"{run_condition.__class__}")
        else:
            print("Runtime not started yet.")

    def wait(self):
        """
        Waits for RuntimeServices to send Response
        """
        self._get_resp_for_run()

    def pause(self):
        """
        Pauses a add_ports_for_polling
        """
        if self._is_running:
            for send_port in self.runtime_to_service:
                send_port.send(MGMT_COMMAND.PAUSE)
            for recv_port in self.service_to_runtime:
                data = recv_port.recv()
                if not enum_equal(data, MGMT_RESPONSE.PAUSED):
                    if enum_equal(data, MGMT_RESPONSE.ERROR):
                        # Receive all errors from the ProcessModels
                        error_cnt = 0
                        for actors in \
                                self._messaging_infrastructure.actors:
                            actors.join()
                            if actors.exception:
                                _, traceback = actors.exception
                                print(traceback)
                                error_cnt += 1
                        self.stop()
                        raise RuntimeError(
                            f"{error_cnt} Exception(s) occurred. See "
                            f"output above for details.")
            self._is_running = False

    def stop(self):
        """Stops an ongoing or paused add_ports_for_polling."""
        try:
            if self._is_started:
                for send_port in self.runtime_to_service:
                    send_port.send(MGMT_COMMAND.STOP)
                for recv_port in self.service_to_runtime:
                    data = recv_port.recv()
                    if not enum_equal(data, MGMT_RESPONSE.TERMINATED):
                        raise RuntimeError(f"Runtime Received {data}")
                self.join()
                self._is_running = False
                self._is_started = False
                # Send messages to RuntimeServices to stop as soon as possible.
            else:
                print("Runtime not started yet.")
        finally:
            self._messaging_infrastructure.stop()

    def join(self):
        """Join all ports and processes"""
        for port in self.runtime_to_service:
            port.join()
        for port in self.service_to_runtime:
            port.join()

    def set_var(self, var_id: int, value: np.ndarray, idx: np.ndarray = None):
        """Sets value of a variable with id 'var_id'."""
        node_config: NodeConfig = self._executable.node_configs[0]
        ev: AbstractExecVar = node_config.exec_vars[var_id]
        runtime_srv_id: int = ev.runtime_srv_id
        model_id: int = ev.process.id

        if issubclass(list(self._executable.rs_builders.values())
                      [runtime_srv_id].rs_class, AsyncPyRuntimeService):
            raise RuntimeError("Set is not supported in AsyncPyRuntimeService")

        if self._is_started:
            # Send a msg to runtime service given the rs_id that you need value
            # from a model with model_id and var with var_id

            # 1. Send SET Command
            req_port: CspSendPort = self.runtime_to_service[runtime_srv_id]
            req_port.send(MGMT_COMMAND.SET_DATA)
            req_port.send(enum_to_np(model_id))
            req_port.send(enum_to_np(var_id))

            # 2. Reshape the data
            buffer: np.ndarray = value
            if idx:
                buffer = buffer[idx]
            buffer_shape: ty.Tuple[int, ...] = buffer.shape
            num_items: int = np.prod(buffer_shape).item()
            buffer = buffer.reshape((1, num_items))

            # 3. Send [NUM_ITEMS, DATA1, DATA2, ...]
            data_port: CspSendPort = self.runtime_to_service[runtime_srv_id]
            data_port.send(enum_to_np(num_items))
            for i in range(num_items):
                data_port.send(enum_to_np(buffer[0, i], np.float64))
        else:
            raise RuntimeError("Runtime has not started")

    def get_var(self, var_id: int, idx: np.ndarray = None) -> np.ndarray:
        """Gets value of a variable with id 'var_id'."""
        node_config: NodeConfig = self._executable.node_configs[0]
        ev: AbstractExecVar = node_config.exec_vars[var_id]
        runtime_srv_id: int = ev.runtime_srv_id
        model_id: int = ev.process.id

        rs_builders = list(self._executable.rs_builders.values())
        rs_class = [rs for rs in rs_builders
                    if rs.runtime_service_id == runtime_srv_id][0].rs_class
        if issubclass(rs_class, AsyncPyRuntimeService):
            raise RuntimeError("Get is not supported in AsyncPyRuntimeService")

        if self._is_started:
            # Send a msg to runtime service given the rs_id that you need value
            # from a model with model_id and var with var_id

            # 1. Send GET Command
            req_port: CspSendPort = self.runtime_to_service[runtime_srv_id]
            req_port.send(MGMT_COMMAND.GET_DATA)
            req_port.send(enum_to_np(model_id))
            req_port.send(enum_to_np(var_id))

            # 2. Receive Data [NUM_ITEMS, DATA1, DATA2, ...]
            data_port: CspRecvPort = self.service_to_runtime[runtime_srv_id]
            num_items: int = int(data_port.recv()[0].item())
            buffer: np.ndarray = np.empty((1, num_items))
            for i in range(num_items):
                buffer[0, i] = data_port.recv()[0]

            # 3. Reshape result and return
            buffer = buffer.reshape(ev.shape)
            if idx:
                return buffer[idx]
            else:
                return buffer
        else:
            raise RuntimeError("Runtime has not started")
