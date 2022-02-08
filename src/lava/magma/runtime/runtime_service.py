# Copyright (C) 2021 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause
# See: https://spdx.org/licenses/
import typing as ty
from abc import ABC, abstractmethod

import numpy as np

from lava.magma.compiler.channels.pypychannel import CspRecvPort, CspSendPort, \
    CspSelector
from lava.magma.core.sync.protocol import AbstractSyncProtocol
from lava.magma.runtime.mgmt_token_enums import (
    enum_to_np,
    enum_equal,
    MGMT_RESPONSE,
    MGMT_COMMAND,
)


class AbstractRuntimeService(ABC):
    def __init__(self, protocol):
        self.protocol: ty.Optional[AbstractSyncProtocol] = protocol

        self.runtime_service_id: ty.Optional[int] = None

        self.runtime_to_service: ty.Optional[CspRecvPort] = None
        self.service_to_runtime: ty.Optional[CspSendPort] = None

        self.model_ids: ty.List[int] = []

        self.service_to_process: ty.Iterable[CspSendPort] = []
        self.process_to_service: ty.Iterable[CspRecvPort] = []

    def __repr__(self):
        return f"Synchronizer : {self.__class__}, \
                 RuntimeServiceId : {self.runtime_service_id}, \
                 Protocol: {self.protocol}"

    def start(self):
        self.runtime_to_service.start()
        self.service_to_runtime.start()
        for i in range(len(self.service_to_process)):
            self.service_to_process[i].start()
            self.process_to_service[i].start()
        self.run()

    @abstractmethod
    def run(self):
        pass

    def join(self):
        self.runtime_to_service.join()
        self.service_to_runtime.join()

        for i in range(len(self.service_to_process)):
            self.service_to_process[i].join()
            self.process_to_service[i].join()


class PyRuntimeService(AbstractRuntimeService):
    pass


class CRuntimeService(AbstractRuntimeService):
    pass


class LoihiPyRuntimeService(PyRuntimeService):
    """RuntimeService that implements Loihi SyncProtocol in Python."""

    def __init__(self, protocol):
        super().__init__(protocol)
        self.req_pre_lrn_mgmt = False
        self.req_post_lrn_mgmt = False
        self.req_lrn = False
        self.req_stop = False
        self.req_pause = False
        self.paused = False
        self._error = False

    class Phase:
        SPK = enum_to_np(1)
        PRE_MGMT = enum_to_np(2)
        LRN = enum_to_np(3)
        POST_MGMT = enum_to_np(4)
        HOST = enum_to_np(5)

    class PMResponse:
        STATUS_DONE = enum_to_np(0)
        """Signfies Ack or Finished with the Command"""
        STATUS_TERMINATED = enum_to_np(-1)
        """Signifies Termination"""
        STATUS_ERROR = enum_to_np(-2)
        """Signifies Error raised"""
        STATUS_PAUSED = enum_to_np(-3)
        """Signifies Execution State to be Paused"""
        REQ_PRE_LRN_MGMT = enum_to_np(-4)
        """Signifies Request of PREMPTION"""
        REQ_LEARNING = enum_to_np(-5)
        """Signifies Request of LEARNING"""
        REQ_POST_LRN_MGMT = enum_to_np(-6)
        """Signifies Request of PREMPTION"""
        REQ_PAUSE = enum_to_np(-7)
        """Signifies Request of PAUSE"""
        REQ_STOP = enum_to_np(-8)
        """Signifies Request of STOP"""

    def _next_phase(self, is_last_time_step: bool):
        """Advances the current phase to the next phase.
        On the first time step it starts with HOST phase and advances to SPK.
        Afterwards it loops: SPK -> PRE_MGMT -> LRN -> POST_MGMT -> SPK
        On the last time step POST_MGMT advances to HOST phase."""
        if self.req_pre_lrn_mgmt:
            self.req_pre_lrn_mgmt = False
            return LoihiPyRuntimeService.Phase.PRE_MGMT
        if self.req_post_lrn_mgmt:
            self.req_post_lrn_mgmt = False
            return LoihiPyRuntimeService.Phase.POST_MGMT
        if self.req_lrn:
            self.req_lrn = False
            return LoihiPyRuntimeService.Phase.LRN
        if self.req_pause:
            self.req_pause = False
            return MGMT_COMMAND.PAUSE
        if self.req_stop:
            self.req_stop = False
            return MGMT_COMMAND.STOP

        if is_last_time_step:
            return LoihiPyRuntimeService.Phase.HOST
        return LoihiPyRuntimeService.Phase.SPK

    def _send_pm_cmd(self, phase: MGMT_COMMAND):
        """Sends a command (phase information) to all ProcessModels."""
        for send_port in self.service_to_process:
            send_port.send(phase)

    def _send_pm_req_given_model_id(self, model_id: int, *requests):
        """Sends requests to a ProcessModel given by the model id."""
        process_idx = self.model_ids.index(model_id)
        req_port = self.service_to_process[process_idx]
        for request in requests:
            req_port.send(request)

    def _get_pm_resp(self) -> ty.Iterable[MGMT_RESPONSE]:
        """Retrieves responses of all ProcessModels."""
        rcv_msgs = []
        num_responses_expected = len(self.model_ids)
        counter = 0
        while counter < num_responses_expected:
            ptos_recv_port = self.process_to_service[counter]
            rcv_msgs.append(ptos_recv_port.recv())
            counter += 1
        for recv_msg in rcv_msgs:
            if enum_equal(recv_msg,
                          LoihiPyRuntimeService.PMResponse.STATUS_ERROR):
                self._error = True
            if enum_equal(recv_msg,
                          LoihiPyRuntimeService.PMResponse.REQ_PRE_LRN_MGMT):
                self.req_pre_lrn_mgmt = True
            if enum_equal(recv_msg,
                          LoihiPyRuntimeService.PMResponse.REQ_POST_LRN_MGMT):
                self.req_post_lrn_mgmt = True
            if enum_equal(recv_msg,
                          LoihiPyRuntimeService.PMResponse.REQ_LEARNING):
                self.req_lrn = True
            if enum_equal(recv_msg,
                          LoihiPyRuntimeService.PMResponse.REQ_PAUSE):
                self.req_pause = True
            if enum_equal(recv_msg,
                          LoihiPyRuntimeService.PMResponse.REQ_STOP):
                self.req_stop = True
            return rcv_msgs

    def _relay_to_runtime_data_given_model_id(self, model_id: int):
        """Relays data received from ProcessModel given by model id  to the
        runtime"""
        process_idx = self.model_ids.index(model_id)
        data_recv_port = self.process_to_service[process_idx]
        data_relay_port = self.service_to_runtime
        num_items = data_recv_port.recv()
        data_relay_port.send(num_items)
        for i in range(int(num_items[0])):
            value = data_recv_port.recv()
            data_relay_port.send(value)

    def _relay_to_pm_data_given_model_id(self, model_id: int):
        """Relays data received from the runtime to the ProcessModel given by
        the model id."""
        process_idx = self.model_ids.index(model_id)

        data_recv_port = self.runtime_to_service
        data_relay_port = self.service_to_process[process_idx]
        # Receive and relay number of items
        num_items = data_recv_port.recv()
        data_relay_port.send(num_items)
        # Receive and relay data1, data2, ...
        for i in range(int(num_items[0].item())):
            data_relay_port.send(data_recv_port.recv())

    def _relay_pm_ack_given_model_id(self, model_id: int):
        """Relays ack received from ProcessModel given by model id to the
        runtime."""
        process_idx = self.model_ids.index(model_id)

        ack_recv_port = self.process_to_service[process_idx]
        ack_relay_port = self.service_to_runtime
        ack_relay_port.send(ack_recv_port.recv())

    def _handle_pause(self):
        # Inform all ProcessModels about the PAUSE command
        self._send_pm_cmd(MGMT_COMMAND.PAUSE)
        rsps = self._get_pm_resp()
        for rsp in rsps:
            if not enum_equal(rsp,
                              LoihiPyRuntimeService.PMResponse.STATUS_PAUSED):
                raise ValueError(f"Wrong Response Received : {rsp}")
        # Inform the runtime about successful pausing
        self.service_to_runtime.send(MGMT_RESPONSE.PAUSED)

    def _handle_stop(self):
        # Inform all ProcessModels about the STOP command
        self._send_pm_cmd(MGMT_COMMAND.STOP)
        rsps = self._get_pm_resp()
        for rsp in rsps:
            if not enum_equal(rsp,
                              LoihiPyRuntimeService.PMResponse.STATUS_TERMINATED
                              ):
                raise ValueError(f"Wrong Response Received : {rsp}")
        # Inform the runtime about successful termination
        self.service_to_runtime.send(MGMT_RESPONSE.TERMINATED)
        self.join()

    def run(self):
        """Retrieves commands from the runtime. On STOP or PAUSE commands all
        ProcessModels are notified and expected to TERMINATE or PAUSE,
        respectively. Otherwise the number of time steps is received as command.
        In this case iterate through the phases of the Loihi protocol until the
        last time step is reached. The runtime is informed after the last time
        step. The loop ends when receiving the STOP command from the runtime."""
        selector = CspSelector()
        phase = LoihiPyRuntimeService.Phase.HOST

        channel_actions = [(self.runtime_to_service, lambda: 'cmd')]

        while True:
            # Probe if there is a new command from the runtime
            action = selector.select(*channel_actions)
            if action == 'cmd':
                command = self.runtime_to_service.recv()
                if enum_equal(command, MGMT_COMMAND.STOP):
                    self._handle_stop()
                    return
                elif enum_equal(command, MGMT_COMMAND.PAUSE):
                    self._handle_pause()
                    self.paused = True
                elif enum_equal(command, MGMT_COMMAND.GET_DATA) or \
                        enum_equal(command, MGMT_COMMAND.SET_DATA):
                    self._handle_get_set(phase, command)
                else:
                    self.paused = False
                    # The number of time steps was received ("command")
                    # Start iterating through Loihi phases
                    curr_time_step = 0
                    phase = LoihiPyRuntimeService.Phase.HOST
                    while True:
                        # Check if it is the last time step
                        is_last_ts = enum_equal(enum_to_np(curr_time_step),
                                                command)
                        # Advance to the next phase
                        phase = self._next_phase(is_last_ts)
                        if enum_equal(phase, MGMT_COMMAND.STOP):
                            self.service_to_runtime.send(MGMT_RESPONSE.REQ_STOP)
                            break
                        if enum_equal(phase, MGMT_COMMAND.PAUSE):
                            self.service_to_runtime.send(
                                MGMT_RESPONSE.REQ_PAUSE)
                            break
                        # Increase time step if spiking phase
                        if enum_equal(phase, LoihiPyRuntimeService.Phase.SPK):
                            curr_time_step += 1
                        # Inform ProcessModels about current phase
                        self._send_pm_cmd(phase)
                        # ProcessModels respond with DONE if not HOST phase
                        if not enum_equal(
                                phase, LoihiPyRuntimeService.Phase.HOST):
                            rcv_msgs = self._get_pm_resp()
                            if self._error:
                                # Forward error to runtime
                                self.service_to_runtime.send(
                                    MGMT_RESPONSE.ERROR)
                                # stop all other pm
                                self._send_pm_cmd(MGMT_COMMAND.STOP)
                                return
                        # Check if pause or stop received from Runtime
                        # TODO: Do we actualy need to wait for PMs to be in
                        # HOST or MGMT phase to stop or pause them?
                        if self.runtime_to_service.probe():
                            cmd = self.runtime_to_service.peek()
                            if enum_equal(cmd, MGMT_COMMAND.STOP):
                                self.runtime_to_service.recv()
                                self._handle_stop()
                                return
                            if enum_equal(cmd, MGMT_COMMAND.PAUSE):
                                self.runtime_to_service.recv()
                                self._handle_pause()
                                self.paused = True
                                break

                        # If HOST phase (last time step ended) break the loop
                        if enum_equal(
                                phase, LoihiPyRuntimeService.Phase.HOST):
                            break
                    if self.paused or enum_equal(phase, MGMT_COMMAND.STOP) or \
                            enum_equal(phase, MGMT_COMMAND.PAUSE):
                        continue
                    # Inform the runtime that last time step was reached
                    self.service_to_runtime.send(MGMT_RESPONSE.DONE)

    def _handle_get_set(self, phase, command):
        if enum_equal(phase, LoihiPyRuntimeService.Phase.HOST):
            if enum_equal(command, MGMT_COMMAND.GET_DATA):
                requests: ty.List[np.ndarray] = [command]
                # recv model_id
                model_id: int = int(self.runtime_to_service.recv()[0].item())
                # recv var_id
                requests.append(self.runtime_to_service.recv())
                self._send_pm_req_given_model_id(model_id, *requests)
                self._relay_to_runtime_data_given_model_id(model_id)
            elif enum_equal(command, MGMT_COMMAND.SET_DATA):
                requests: ty.List[np.ndarray] = [command]
                # recv model_id
                model_id: int = int(self.runtime_to_service.recv()[0].item())
                # recv var_id
                requests.append(self.runtime_to_service.recv())
                self._send_pm_req_given_model_id(model_id, *requests)
                self._relay_to_pm_data_given_model_id(model_id)
            else:
                raise RuntimeError(f"Unknown request {command}")


class LoihiCRuntimeService(AbstractRuntimeService):
    """RuntimeService that implements Loihi SyncProtocol in C."""
    pass

class AsyncPyRuntimeService(PyRuntimeService):
    """RuntimeService that implements Async SyncProtocol in Py."""

    def __init__(self, protocol):
        super().__init__(protocol)
        self.req_stop = False
        self.req_pause = False
        self._error = False

    class PMResponse:
        STATUS_DONE = enum_to_np(0)
        """Signfies Ack or Finished with the Command"""
        STATUS_TERMINATED = enum_to_np(-1)
        """Signifies Termination"""
        STATUS_ERROR = enum_to_np(-2)
        """Signifies Error raised"""
        STATUS_PAUSED = enum_to_np(-3)
        """Signifies Execution State to be Paused"""
        REQ_PAUSE = enum_to_np(-4)
        """Signifies Request of PAUSE"""
        REQ_STOP = enum_to_np(-5)
        """Signifies Request of STOP"""

    def _send_pm_cmd(self, cmd: MGMT_COMMAND):
        for stop_send_port in self.service_to_process:
            stop_send_port.send(cmd)

    def _get_pm_resp(self) -> ty.Iterable[MGMT_RESPONSE]:
        rcv_msgs = []
        for ptos_recv_port in self.process_to_service:
            rcv_msgs.append(ptos_recv_port.recv())
        return rcv_msgs

    def _handle_pause(self):
        # Inform the runtime about successful pausing
        self.service_to_runtime.send(MGMT_RESPONSE.PAUSED)

    def _handle_stop(self):
        self._send_pm_cmd(MGMT_COMMAND.STOP)
        rsps = self._get_pm_resp()
        for rsp in rsps:
            if not enum_equal(rsp,
                              LoihiPyRuntimeService.PMResponse.STATUS_TERMINATED
                              ):
                self.service_to_runtime.send(MGMT_RESPONSE.ERROR)
                raise ValueError(f"Wrong Response Received : {rsp}")
        # Inform the runtime about successful termination
        self.service_to_runtime.send(MGMT_RESPONSE.TERMINATED)
        self.join()

    def run(self):
        selector = CspSelector()
        channel_actions = [(self.runtime_to_service, lambda: 'cmd')]
        while True:
            # Probe if there is a new command from the runtime
            action = selector.select(*channel_actions)
            channel_actions = []
            if action == 'cmd':
                command = self.runtime_to_service.recv()
                if enum_equal(command, MGMT_COMMAND.STOP):
                    self._handle_stop()
                    return
                elif enum_equal(command, MGMT_COMMAND.PAUSE):
                    self._handle_pause()
                else:
                    self._send_pm_cmd(MGMT_COMMAND.RUN)
                    for ptos_recv_port in self.process_to_service:
                        channel_actions.append((ptos_recv_port,
                                                lambda: 'resp'))
            elif action == 'resp':
                resps = self._get_pm_resp()
                for resp in resps:
                    if enum_equal(resp,
                                  AsyncPyRuntimeService.PMResponse.REQ_PAUSE):
                        self.req_pause = True
                    if enum_equal(resp,
                                  AsyncPyRuntimeService.PMResponse.REQ_STOP):
                        self.req_stop = True
                    if enum_equal(resp,
                                  AsyncPyRuntimeService.PMResponse.STATUS_ERROR):
                        self._error = True
                if self.req_stop:
                    self.service_to_runtime.send(MGMT_RESPONSE.REQ_STOP)
                if self.req_pause:
                    self.service_to_runtime.send(MGMT_RESPONSE.REQ_PAUSE)
                if self._error:
                    self.service_to_runtime.send(MGMT_RESPONSE.ERROR)
            channel_actions.append((self.runtime_to_service, lambda: 'cmd'))
