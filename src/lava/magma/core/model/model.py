# Copyright (C) 2021 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause
# See: https://spdx.org/licenses/
from __future__ import annotations

import typing as ty
from abc import ABC

if ty.TYPE_CHECKING:
    from lava.magma.core.process.process import AbstractProcess
from lava.magma.core.resources import AbstractResource
from lava.magma.core.sync.protocol import AbstractSyncProtocol


class AbstractProcessModel(ABC):
    """Represents a model that implements the behavior of a Process.

    ProcessModels are usually not instantiated by the user directly but by
    the compiler. ProcessModels are expected to have the same variables and
    ports as those defined in the Process but with an implementation specific to
    the ProcessModel. I.e. in a PyProcessModel, a Var will be implemented by
    a np.ndarray and a Port might be implemented with a PyInputPort.
    The compiler is supposed to instantiate these ProcModels and initialize
    those vars and ports given initial values from the Process and
    implementation details from the ProcModel.
    For transparency, class attributes and their types should be
    explicitly defined upfront by the developer of a ProcModel to avoid lint
    warnings due to unresolved variables or unknown or illegal types.

    This is a proposal of a low-boilerplate code convention to achieve this:

    1. The same Vars and Ports as defined in the Process must be defined as
    class variables in the ProcessModels.
    2. These class variables should be initialized with LavaType objects.
    LavaTypes specify the future class-type of this Var or Port, the numeric
    d_type and precision and maybe dynamic range if different from what would be
    implied by d_type. The compiler will later read these LavaTypes defined
    at the class level to initialize concrete class objects from the initial
    values provided in the Process. During this process, the compiler will
    create object level attributes with the same name as the class level
    variables. This should not cause problems as class level and instance
    level attributes can co-exist. However, instance level attributes shadow
    class level attributes with the same name if they exist.
    3. Direct type annotations should be used equal to the class type in
    the LavaType to suppress type warnings in the rest of the class code
    although this leads to a bit of verbosity in the end. We could leave out
    the class type in the LavaType and infer it from
    ProcModel.__annotations__ if the user has not forgotten to specify it.
    """

    implements_process: ty.Optional[ty.Type[AbstractProcess]] = None
    implements_protocol: ty.Optional[ty.Type[AbstractSyncProtocol]] = None
    required_resources: ty.List[ty.Type[AbstractResource]] = []
    tags: ty.List[str] = []

    def __repr__(self):
        pm_name = self.__class__.__qualname__
        p_name = self.implements_process.__qualname__
        dev_names = " ".join([d.__qualname__ for d in self.required_resources])
        tags = ", ".join([t.__qualname__ for t in self.tags])
        return (
                pm_name
                + " implements "
                + p_name
                + "\n"
                + " " * len(pm_name)
                + " supports   "
                + dev_names
                + "\n"
                + " " * len(pm_name)
                + " has tags   "
                + tags
        )

    def run(self):
        raise NotImplementedError("'run' method is not implemented.")

    def add_ports_for_polling(self):
        raise NotImplementedError(
            "'add_ports_for_polling' method is not implemented.")

    # ToDo: What does this function do here? The AbstractProcModel can't
    #  depend on one specific Python implementation of ports/channels. It can
    #  probably not even have a start function. Because for a CProcModel
    #  running on LMT there might not even be Python start function to call.
    #  Starting the ports is likely the RuntimeService's or Builder's job
    #  which is what makes a process run on a certain compute resource.
    def start(self):
        # Store the list of csp_ports. Start them here.
        raise NotImplementedError
