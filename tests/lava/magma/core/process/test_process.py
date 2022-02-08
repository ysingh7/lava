# Copyright (C) 2021 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause
# See: https://spdx.org/licenses/
import unittest

from lava.magma.core.process.process import (
    AbstractProcess,
    Collection,
    ProcessServer,
)
from lava.magma.core.process.variable import Var
from lava.magma.core.process.ports.ports import (
    InPort,
    OutPort,
    RefPort,
    VarPort,
)
from lava.magma.core.run_conditions import RunSteps, RunContinuous


class MinimalProcess(AbstractProcess):
    """The most minimal process has no Vars or Ports."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)


class TestCollection(unittest.TestCase):
    def setUp(self):
        """Reset ProcessServer before each test."""
        ProcessServer().reset_server()

    def test_constuctor(self):
        """Check that Collection can be constructed."""

        # Create a Collection
        p = MinimalProcess()
        c = Collection(p, "TestCollection")

        self.assertIsInstance(c, Collection)
        self.assertEqual(c.name, "TestCollection")
        self.assertTrue(c.is_empty())

    def test_set_members(self):
        """Checks setting of collection members."""

        # Create a Collection
        p = MinimalProcess()
        c = Collection(p, "TestCollection")

        # Create some Vars
        v1 = Var((1,))
        v2 = Var((2,))
        c.add_members({"v1": v1, "v2": v2})

        # Check members can be accessed
        self.assertEqual(len(c.members), 2)
        self.assertEqual(c.member_names, ["v1", "v2"])
        self.assertEqual(c.v1, v1)
        self.assertEqual(c.v2, v2)

        # Check iterator
        for m in c:
            self.assertIsInstance(m, Var)


class TestProcessSetup(unittest.TestCase):
    def setUp(self):
        """Reset ProcessServer before each test."""
        ProcessServer().reset_server()

    def test_id_and_name(self):
        """Check name and id generation by ProcessServer."""

        # Create a first process
        p1 = MinimalProcess()

        # Check it's really a process
        self.assertTrue(p1, AbstractProcess)

        # At this point, a process has no assigned ProcModel or Runtime yet
        self.assertIsNone(p1._model)
        self.assertIsNone(p1.runtime)

        # A process gets a globally unique id from a global ProcessServer
        self.assertEqual(p1.id, 0)

        # Without explicitly assigning a name to the process, the name is
        # auto-generated
        self.assertEqual(p1.name, "Process_0")

        # Create another process
        p2 = MinimalProcess(name="Second process")

        # The globally unique id gets incremented
        self.assertEqual(p2.id, 1)

        # But if a name is provided it gets used
        self.assertEqual(p2.name, "Second process")

        # The ProcessServer is a global singleton and holds references to all
        # processes created so far
        ps = ProcessServer()
        self.assertEqual(ps.processes, [p1, p2])

        # The ProcessServer holds the process id that's going to be assigned
        # next
        self.assertEqual(ps._next_id, 2)

        # The ProcessServer can also be reset
        ps.reset_server()
        self.assertEqual(ps._next_id, 0)
        self.assertEqual(len(ps.processes), 0)

    def test_constructor_with_arguments(self):
        """Checks passing of arguments to constructor."""

        # Call process constructor with arguments
        p = MinimalProcess(a=1, b=2)

        # Constructor arguments can be used for initializing variables and
        # ports and will be stored for later usage as _init_args
        self.assertIsInstance(p.init_args, dict)
        self.assertEqual(p.init_args["a"], 1)
        self.assertEqual(p.init_args["b"], 2)

    def test_process_without_vars_or_ports(self):
        """Checks that Collections get initialized."""

        # Create minimal process without vars or ports
        p = MinimalProcess()

        # A process has collections for InPorts, OutPorts, VarPors, RefPorts
        # and Vars which group these objects created during initialization but
        # they all start out empty
        self.assertTrue(p.in_ports.is_empty())
        self.assertTrue(p.out_ports.is_empty())
        self.assertTrue(p.var_ports.is_empty())
        self.assertTrue(p.ref_ports.is_empty())
        self.assertTrue(p.vars.is_empty())
        self.assertTrue(p.procs.is_empty())

    def test_process_with_vars_and_ports(self):
        """Checks process with Vars and Ports can be constructed."""

        # A non-empty process
        class Proc(AbstractProcess):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.in_port1 = InPort((1,))
                self.v1 = Var((1,))
                self.v2 = Var((1,))
                self.out_port1 = OutPort((1,))
                self.out_port2 = OutPort((1,))
                self.ref_port1 = RefPort((10,))
                # Expose 'v2' explicitly as a VarPort for shared memory access
                self.var_port1 = VarPort(self.v2)

        # Create a process
        p = Proc()

        # Post initialization, all Vars and Ports will be automatically put
        # in corresponding collections
        self.assertEqual(p.in_ports.member_names, ["in_port1"])
        self.assertEqual(p.vars.member_names, ["v1", "v2"])
        self.assertEqual(p.out_ports.member_names, ["out_port1", "out_port2"])
        self.assertEqual(p.var_ports.member_names, ["var_port1"])
        self.assertEqual(p.ref_ports.member_names, ["ref_port1"])

        # Variables will have gotten their names assigned
        self.assertEqual(p.v1.name, "v1")
        self.assertEqual(p.out_ports.out_port2.name, "out_port2")

        # The parent Process will also have been assigned to variables and ports
        self.assertEqual(p.v2.process, p)
        self.assertEqual(p.in_port1.process, p)

        # Process has no sub processes (yet)
        self.assertTrue(p.procs.is_empty())

        # Since 'v2' has been explicitly exposed for shared memory access,
        # 'var_port1' contains a reference to it
        self.assertEqual(p.var_ports.var_port1.var, p.v2)

    def test_register_sub_procs(self):
        """Checks registration of sub processes of a process.

        The behavior of a Process can, among other methods, be implemented
        via other sub processes via a SubProcessModel. How this works is
        outside of the scope of this unit test and will be covered in other
        unit tests. But if that's the case, parent and sub processes are
        mutually registered with each other via the Compiler.

        Only sub process registration is tested here.
        """

        # A minimal Process
        class Proc(AbstractProcess):
            pass

        # To test sub process registration, let's create a parent and a few
        # sub processes.
        parent_proc = Proc()
        # ... it does not matter whether the sub processes have the same type
        sub_procs = [Proc(), Proc(), Proc()]

        # Before 'registration' the parent's 'procs' are emtpy
        self.assertTrue(parent_proc.procs.is_empty())
        # ... and the sub processes have no 'parent_proc'
        for p in sub_procs:
            self.assertIsNone(p.parent_proc)

        # 'Registration' registers parent and sub processes with each other
        parent_proc.register_sub_procs({p.name: p for p in sub_procs})

        # After 'registration' the parent's 'procs' are no longer empty...
        self.assertFalse(parent_proc.procs.is_empty())

        # ... and the 'procs' collection contains the sub processes
        self.assertEqual(parent_proc.procs.members, sub_procs)

        # In addition, each sub process's 'parent_proc' is now set
        for p in sub_procs:
            self.assertEqual(p.parent_proc, parent_proc)

    def test_is_sub_proc_of(self):
        """Checks whether determination whether one process is a sub process
        of another process.

        As before, how sub processes are created is outside the scope of this
        unit test. Here we just check the determination of a sub process
        relationship.
        """

        # A minimal Process
        class Proc(AbstractProcess):
            pass

        # Let's define the following hierarchy of process
        # proc1
        #   -> proc2
        #        -> proc3
        #        -> proc4
        #   -> proc5
        proc1, proc2, proc3, proc4, proc5 = (Proc() for _ in range(5))
        proc2.register_sub_procs({p.name: p for p in [proc3, proc4]})
        proc1.register_sub_procs({p.name: p for p in [proc2, proc5]})

        # We can check across hierarchical levels if one process is a sub
        # process of another process.
        # proc3 and proc4 are both sub processes of proc2 and proc1
        self.assertTrue(proc3.is_sub_proc_of(proc2))
        self.assertTrue(proc4.is_sub_proc_of(proc2))
        self.assertTrue(proc3.is_sub_proc_of(proc1))
        self.assertTrue(proc4.is_sub_proc_of(proc1))
        # Similarly, proc5 is a sub process of proc1
        self.assertTrue(proc5.is_sub_proc_of(proc1))
        # But for instance proc3 is not a sub process of proc5...
        self.assertFalse(proc3.is_sub_proc_of(proc5))
        # ... nor is any other random process a sub process of proc1
        yet_another_proc = Proc()
        self.assertFalse(yet_another_proc.is_sub_proc_of(proc1))

if __name__ == "__main__":
    unittest.main()
