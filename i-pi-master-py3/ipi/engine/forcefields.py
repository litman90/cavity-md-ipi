"""Contains the classes that connect the driver to the python code.

ForceField objects are force providers, i.e. they are the abstraction
layer for a driver that gets positions and returns forces (and energy).
"""

# This file is part of i-PI.
# i-PI Copyright (C) 2014-2015 i-PI developers
# See the "licenses" directory for full license information.


import time
import threading

import numpy as np

from ipi.utils.softexit import softexit
from ipi.utils.messages import verbosity
from ipi.utils.messages import info
from ipi.interfaces.sockets import InterfaceSocket
from ipi.utils.depend import dobject
from ipi.utils.depend import dstrip
from ipi.utils.io import read_file
from ipi.utils.units import unit_to_internal, unit_to_user, UnitMap
from ipi.interfaces.cavphsockets import InterfaceCavPhSocket
from ipi.interfaces.photons import photons

try:
    import plumed
except:
    plumed = None

import os

class ForceRequest(dict):

    """An extension of the standard Python dict class which only has a == b
    if a is b == True, rather than if the elements of a and b are identical.

    Standard dicts are checked for equality if elements have the same value.
    Here I only care if requests are instances of the very same object.
    This is useful for the `in` operator, which uses equality to test membership.
    """

    def __eq__(self, y):
        """Overwrites the standard equals function."""
        return self is y


class ForceField(dobject):

    """Base forcefield class.

    Gives the standard methods and quantities needed in all the forcefield
    classes.

    Attributes:
        pars: A dictionary of the parameters needed to initialize the forcefield.
            Of the form {'name1': value1, 'name2': value2, ... }.
        name: The name of the forcefield.
        latency: A float giving the number of seconds the socket will wait
            before updating the client list.
        requests: A list of all the jobs to be given to the client codes.
        dopbc: A boolean giving whether or not to apply the periodic boundary
            conditions before sending the positions to the client code.
        _thread: The thread on which the socket polling loop is being run.
        _doloop: A list of booleans. Used to decide when to stop running the
            polling loop.
        _threadlock: Python handle used to lock the thread held in _thread.
    """

    def __init__(self, latency=1.0, name="", pars=None, dopbc=True, active=np.array([-1]), threaded=False):
        """Initialises ForceField.

        Args:
            latency: The number of seconds the socket will wait before updating
                the client list.
            name: The name of the forcefield.
            pars: A dictionary used to initialize the forcefield, if required.
                Of the form {'name1': value1, 'name2': value2, ... }.
            dopbc: Decides whether or not to apply the periodic boundary conditions
                before sending the positions to the client code.
            active: Indexes of active atoms in this forcefield
        """

        if pars is None:
            self.pars = {}
        else:
            self.pars = pars

        self.name = name
        self.latency = latency
        self.requests = []
        self.dopbc = dopbc
        self.active = active
        self.iactive = None
        self.threaded = threaded
        self._thread = None
        self._doloop = [False]
        self._threadlock = threading.Lock()

    def queue(self, atoms, cell, reqid=-1):
        """Adds a request.

        Note that the pars dictionary need to be sent as a string of a
        standard format so that the initialisation of the driver can be done.

        Args:
            atoms: An Atoms object giving the atom positions.
            cell: A Cell object giving the system box.
            pars: An optional dictionary giving the parameters to be sent to the
                driver for initialisation. Defaults to {}.
            reqid: An optional integer that identifies requests of the same type,
               e.g. the bead index

        Returns:
            A list giving the status of the request of the form {'pos': An array
            giving the atom positions folded back into the unit cell,
            'cell': Cell object giving the system box, 'pars': parameter string,
            'result': holds the result as a list once the computation is done,
            'status': a string labelling the status of the calculation,
            'id': the id of the request, usually the bead number, 'start':
            the starting time for the calculation, used to check for timeouts.}.
        """

        par_str = " "

        if not self.pars is None:
            for k, v in list(self.pars.items()):
                par_str += k + " : " + str(v) + " , "
        else:
            par_str = " "

        pbcpos = dstrip(atoms.q).copy()

        # Indexes come from input in a per atom basis and we need to make a per atom-coordinate basis
        # Reformat indexes for full system (default) or piece of system
        # active atoms do not change but we only know how to build this array once we get the positions once
        if self.iactive is None:
            if self.active[0] == -1:
                activehere = np.arange(len(pbcpos))
            else:
                activehere = np.array([[3 * n, 3 * n + 1, 3 * n + 2] for n in self.active])

            # Reassign active indexes in order to use them
            activehere = activehere.flatten()

            # Perform sanity check for active atoms
            if (len(activehere) > len(pbcpos) or activehere[-1] > (len(pbcpos) - 1)):
                raise ValueError("There are more active atoms than atoms!")

            self.iactive = activehere

        if self.dopbc:
            cell.array_pbc(pbcpos)

        newreq = ForceRequest({
            "id": reqid,
            "pos": pbcpos,
            "active": self.iactive,
            "cell": (dstrip(cell.h).copy(), dstrip(cell.ih).copy()),
            "pars": par_str,
            "result": None,
            "status": "Queued",
            "start": -1,
            "t_queued": time.time(),
            "t_dispatched": 0,
            "t_finished": 0
        })

        with self._threadlock:
            self.requests.append(newreq)

        if not self.threaded:
            self.poll()

        return newreq

    def poll(self):
        """Polls the forcefield object to check if it has finished."""

        with self._threadlock:
            for r in self.requests:
                if r["status"] == "Queued":
                    r["t_dispatched"] = time.time()
                    r["result"] = [0.0, np.zeros(len(r["pos"]), float), np.zeros((3, 3), float), ""]
                    r["status"] = "Done"
                    r["t_finished"] = time.time()

    def _poll_loop(self):
        """Polling loop.

        Loops over the different requests, checking to see when they have
        finished.
        """

        info(" @ForceField: Starting the polling thread main loop.", verbosity.low)
        while self._doloop[0]:
            time.sleep(self.latency)
            if len(self.requests) > 0:
                self.poll()

    def release(self, request):
        """Shuts down the client code interface thread.

        Args:
            request: The id of the job to release.
        """

        """Frees up a request."""

        with self._threadlock:
            if request in self.requests:
                try:
                    self.requests.remove(request)
                except ValueError:
                    print("failed removing request", id(request), ' ', end=' ')
                    print([id(r) for r in self.requests], "@", threading.currentThread())
                    raise

    def stop(self):
        """Dummy stop method."""

        self._doloop[0] = False
        for r in self.requests:
            r["status"] = "Exit"

    def start(self):
        """Spawns a new thread.

        Splits the main program into two threads, one that runs the polling loop
        which updates the client list, and one which gets the data.

        Raises:
            NameError: Raised if the polling thread already exists.
        """

        if not self._thread is None:
            raise NameError("Polling thread already started")

        if self.threaded:
            self._doloop[0] = True
            self._thread = threading.Thread(target=self._poll_loop, name="poll_" + self.name)
            self._thread.daemon = True
            self._thread.start()
            softexit.register_thread(self._thread, self._doloop)
        softexit.register_function(self.softexit)

    def softexit(self):
        """ Takes care of cleaning up upon softexit """

        self.stop()

    def update(self):
        """ Makes updates to the potential that only need to be triggered
        upon completion of a time step. """

        pass


class FFSocket(ForceField):

    """Interface between the PIMD code and a socket for a single replica.

    Deals with an individual replica of the system, obtaining the potential
    force and virial appropriate to this system. Deals with the distribution of
    jobs to the interface.

    Attributes:
        socket: The interface object which contains the socket through which
            communication between the forcefield and the driver is done.
    """

    def __init__(self, latency=1.0, name="", pars=None, dopbc=True,
                 active=np.array([-1]), threaded=True, interface=None):
        """Initialises FFSocket.

        Args:
           latency: The number of seconds the socket will wait before updating
              the client list.
           name: The name of the forcefield.
           pars: A dictionary used to initialize the forcefield, if required.
              Of the form {'name1': value1, 'name2': value2, ... }.
           dopbc: Decides whether or not to apply the periodic boundary conditions
              before sending the positions to the client code.
           interface: The object used to create the socket used to interact
              with the client codes.
        """

        # a socket to the communication library is created or linked
        super(FFSocket, self).__init__(latency, name, pars, dopbc, active, threaded)
        if interface is None:
            self.socket = InterfaceSocket()
        else:
            self.socket = interface
        self.socket.requests = self.requests

    def poll(self):
        """Function to check the status of the client calculations."""

        self.socket.poll()

    def start(self):
        """Spawns a new thread."""

        self.socket.open()
        super(FFSocket, self).start()

    def stop(self):
        """Closes the socket and the thread."""

        super(FFSocket, self).stop()
        if self._thread is not None:
            # must wait until loop has ended before closing the socket
            self._thread.join()
        self.socket.close()


class FFLennardJones(ForceField):

    """Basic fully pythonic force provider.

    Computes LJ interactions without minimum image convention, cutoffs or
    neighbour lists. Parallel evaluation with threads.

    Attributes:
        parameters: A dictionary of the parameters used by the driver. Of the
            form {'name': value}.
        requests: During the force calculation step this holds a dictionary
            containing the relevant data for determining the progress of the step.
            Of the form {'atoms': atoms, 'cell': cell, 'pars': parameters,
                         'status': status, 'result': result, 'id': bead id,
                         'start': starting time}.
    """

    def __init__(self, latency=1.0e-3, name="", pars=None, dopbc=False, threaded=False):
        """Initialises FFLennardJones.

        Args:
           pars: Optional dictionary, giving the parameters needed by the driver.
        """

        # check input - PBCs are not implemented here
        if dopbc:
            raise ValueError("Periodic boundary conditions are not supported by FFLennardJones.")

        # a socket to the communication library is created or linked
        super(FFLennardJones, self).__init__(latency, name, pars, dopbc=dopbc, threaded=threaded)
        self.epsfour = float(self.pars["eps"]) * 4
        self.sixepsfour = 6 * self.epsfour
        self.sigma2 = float(self.pars["sigma"]) * float(self.pars["sigma"])

    def poll(self):
        """Polls the forcefield checking if there are requests that should
        be answered, and if necessary evaluates the associated forces and energy."""

        # We have to be thread-safe, as in multi-system mode this might get
        # called by many threads at once.
        with self._threadlock:
            for r in self.requests:
                if r["status"] == "Queued":
                    r["status"] = "Running"
                    r["t_dispatched"] = time.time()
                    self.evaluate(r)

    def evaluate(self, r):
        """Just a silly function evaluating a non-cutoffed, non-pbc and
        non-neighbour list LJ potential."""

        q = r["pos"].reshape((-1, 3))
        nat = len(q)

        v = 0.0
        f = np.zeros(q.shape)
        for i in range(1, nat):
            dij = q[i] - q[:i]
            rij2 = (dij**2).sum(axis=1)

            x6 = (self.sigma2 / rij2)**3
            x12 = x6**2

            v += (x12 - x6).sum()
            dij *= (self.sixepsfour * (2.0 * x12 - x6) / rij2)[:, np.newaxis]
            f[i] += dij.sum(axis=0)
            f[:i] -= dij

        v *= self.epsfour

        r["result"] = [v, f.reshape(nat * 3), np.zeros((3, 3), float), ""]
        r["status"] = "Done"


class FFDebye(ForceField):

    """Debye crystal harmonic reference potential

    Computes a harmonic forcefield.

    Attributes:
       parameters: A dictionary of the parameters used by the driver. Of the
          form {'name': value}.
       requests: During the force calculation step this holds a dictionary
          containing the relevant data for determining the progress of the step.
          Of the form {'atoms': atoms, 'cell': cell, 'pars': parameters,
                       'status': status, 'result': result, 'id': bead id,
                       'start': starting time}.
    """

    def __init__(self, latency=1.0, name="", H=None, xref=None, vref=0.0, pars=None, dopbc=False, threaded=False):
        """Initialises FFDebye.

        Args:
           pars: Optional dictionary, giving the parameters needed by the driver.
        """

        # a socket to the communication library is created or linked
        # NEVER DO PBC -- forces here are computed without.
        super(FFDebye, self).__init__(latency, name, pars, dopbc=False)

        if H is None:
            raise ValueError("Must provide the Hessian for the Debye crystal.")
        if xref is None:
            raise ValueError("Must provide a reference configuration for the Debye crystal.")

        self.H = H
        self.xref = xref
        self.vref = vref

        eigsys = np.linalg.eigh(self.H)
        info(" @ForceField: Hamiltonian eigenvalues: " + ' '.join(map(str, eigsys[0])), verbosity.medium)

    def poll(self):
        """ Polls the forcefield checking if there are requests that should
        be answered, and if necessary evaluates the associated forces and energy. """

        # we have to be thread-safe, as in multi-system mode this might get called by many threads at once
        with self._threadlock:
            for r in self.requests:
                if r["status"] == "Queued":
                    r["status"] = "Running"
                    self.evaluate(r)

    def evaluate(self, r):
        """ A simple evaluator for a harmonic Debye crystal potential. """

        q = r["pos"]
        n3 = len(q)
        if self.H.shape != (n3, n3):
            raise ValueError("Hessian size mismatch")
        if self.xref.shape != (n3,):
            raise ValueError("Reference structure size mismatch")

        d = q - self.xref
        mf = np.dot(self.H, d)

        r["result"] = [self.vref + 0.5 * np.dot(d, mf), -mf, np.zeros((3, 3), float), ""]
        r["status"] = "Done"
        r["t_finished"] = time.time()


class FFPlumed(ForceField):
    """Direct PLUMED interface

    Computes forces from a PLUMED input.

    Attributes:
        parameters: A dictionary of the parameters used by the driver. Of the
            form {'name': value}.
        requests: During the force calculation step this holds a dictionary
            containing the relevant data for determining the progress of the step.
            Of the form {'atoms': atoms, 'cell': cell, 'pars': parameters,
                      'status': status, 'result': result, 'id': bead id,
                      'start': starting time}.
    """

    def __init__(self, latency=1.0e-3, name="", pars=None, dopbc=False, threaded=False, init_file="", plumeddat="", plumedstep=0):
        """Initialises FFPlumed.

        Args:
           pars: Optional dictionary, giving the parameters needed by the driver.
        """

        # a socket to the communication library is created or linked
        if plumed is None:
            raise ImportError("Cannot find plumed libraries to link to a FFPlumed object/")
        super(FFPlumed, self).__init__(latency, name, pars, dopbc=False, threaded=threaded)
        self.plumed = plumed.Plumed()
        self.plumeddat = plumeddat
        self.plumedstep = plumedstep
        self.init_file = init_file

        if self.init_file.mode == "xyz":
            infile = open(self.init_file.value, "r")
            myframe = read_file(self.init_file.mode, infile)
            myatoms = myframe['atoms']
            mycell = myframe['cell']
            myatoms.q *= unit_to_internal("length", self.init_file.units, 1.0)
            mycell.h *= unit_to_internal("length", self.init_file.units, 1.0)

        self.natoms = myatoms.natoms
        self.plumed.cmd("setNatoms", self.natoms)
        self.plumed.cmd("setPlumedDat", self.plumeddat)
        self.plumed.cmd("setTimestep", 1.)
        self.plumed.cmd("setMDEnergyUnits", 2625.4996)        # Pass a pointer to the conversion factor between the energy unit used in your code and kJ mol-1
        self.plumed.cmd("setMDLengthUnits", 0.052917721)        # Pass a pointer to the conversion factor between the length unit used in your code and nm
        self.plumed.cmd("setMDTimeUnits", 2.4188843e-05)
        self.plumedrestart = False
        if self.plumedstep > 0:
            # we are restarting, signal that PLUMED should continue
            self.plumedrestart = True
            self.plumed.cmd("setRestart", 1)
        self.plumed.cmd("init")
        self.charges = dstrip(myatoms.q) * 0.0
        self.masses = dstrip(myatoms.m)
        self.lastq = np.zeros(3 * self.natoms)

    def poll(self):
        """Polls the forcefield checking if there are requests that should
        be answered, and if necessary evaluates the associated forces and energy."""

        # We have to be thread-safe, as in multi-system mode this might get
        # called by many threads at once.
        with self._threadlock:
            for r in self.requests:
                if r["status"] == "Queued":
                    r["status"] = "Running"
                    r["t_dispatched"] = time.time()
                    self.evaluate(r)
                    r["t_finished"] = time.time()

    def evaluate(self, r):
        """A wrapper function to call the PLUMED evaluation routines
        and return forces."""

        if self.natoms != len(r["pos"]) / 3:
            raise ValueError("Size of atom array changed after initialization of FFPlumed")

        v = 0.0
        f = np.zeros(3 * self.natoms)
        vir = np.zeros((3, 3))

        self.lastq[:] = r["pos"]
        # for the moment these are set to dummy values taken from an init file.
        # linking with the current value in simulations is non-trivial, as masses
        # are not expected to be the force evaluator's business, and charges are not
        # i-PI's business.
        self.plumed.cmd("setStep", self.plumedstep)
        self.plumed.cmd("setCharges", self.charges)
        self.plumed.cmd("setMasses", self.masses)

        # these instead are set properly. units conversion is done on the PLUMED side
        self.plumed.cmd("setBox", r["cell"][0])
        self.plumed.cmd("setPositions", r["pos"])
        self.plumed.cmd("setForces", f)
        self.plumed.cmd("setVirial", vir)
        self.plumed.cmd("prepareCalc");
        self.plumed.cmd("performCalcNoUpdate");

        bias = np.zeros(1, float)
        self.plumed.cmd("getBias", bias)
        v = bias[0]
        vir *= -1

        r["result"] = [v, f, vir, ""]
        r["status"] = "Done"

    def mtd_update(self, pos, cell):
        """ Makes updates to the potential that only need to be triggered
        upon completion of a time step. """

        self.plumedstep += 1
        f = np.zeros(3 * self.natoms)
        vir = np.zeros((3, 3))

        self.plumed.cmd("setStep", self.plumedstep)
        self.plumed.cmd("setCharges", self.charges)
        self.plumed.cmd("setMasses", self.masses)
        self.plumed.cmd("setPositions", pos)
        self.plumed.cmd("setBox", cell)
        self.plumed.cmd("setForces", f)
        self.plumed.cmd("setVirial", vir)
        self.plumed.cmd("prepareCalc");
        self.plumed.cmd("performCalcNoUpdate");
        self.plumed.cmd("update")

        return True


class FFYaff(ForceField):

    """ Use Yaff as a library to construct a force field """

    def __init__(self, latency=1.0, name="", threaded=False, yaffpara=None, yaffsys=None, yafflog='yaff.log', rcut=18.89726133921252, alpha_scale=3.5, gcut_scale=1.1, skin=0, smooth_ei=False, reci_ei='ewald', pars=None, dopbc=False):
        """Initialises FFYaff and enables a basic Yaff force field.

        Args:

           yaffpara: File name of the Yaff parameter file

           yaffsys: File name of the Yaff system file

           yafflog: File name to which Yaff will write some information about the system and the force field

           pars: Optional dictionary, giving the parameters needed by the driver.

           **kwargs: All keyword arguments that can be provided when generating
                     a Yaff force field; see constructor of FFArgs in Yaff code

        """

        from yaff import System, ForceField, log
        import codecs
        import locale
        import atexit

        # a socket to the communication library is created or linked
        super(FFYaff, self).__init__(latency, name, pars, dopbc, threaded=threaded)

        # A bit weird to use keyword argument for a required argument, but this
        # is also done in the code above.
        if yaffpara is None:
            raise ValueError("Must provide a Yaff parameter file.")

        if yaffsys is None:
            raise ValueError("Must provide a Yaff system file.")

        self.yaffpara = yaffpara
        self.yaffsys = yaffsys
        self.rcut = rcut
        self.alpha_scale = alpha_scale
        self.gcut_scale = gcut_scale
        self.skin = skin
        self.smooth_ei = smooth_ei
        self.reci_ei = reci_ei
        self.yafflog = yafflog

        # Open log file
        logf = open(yafflog, 'w')
        # Tell Python to close the file when the script exits
        atexit.register(logf.close)

        # Redirect Yaff log to file
        log._file = codecs.getwriter(locale.getpreferredencoding())(logf)

        self.system = System.from_file(self.yaffsys)
        self.ff = ForceField.generate(self.system, self.yaffpara, rcut=self.rcut, alpha_scale=self.alpha_scale, gcut_scale=self.gcut_scale, skin=self.skin, smooth_ei=self.smooth_ei, reci_ei=self.reci_ei)

        log._active = False

    def poll(self):
        """ Polls the forcefield checking if there are requests that should
        be answered, and if necessary evaluates the associated forces and energy. """

        # we have to be thread-safe, as in multi-system mode this might get called by many threads at once
        with self._threadlock:
            for r in self.requests:
                if r["status"] == "Queued":
                    r["status"] = "Running"
                    self.evaluate(r)

    def evaluate(self, r):
        """ Evaluate the energy and forces with the Yaff force field. """

        q = r["pos"]
        nat = len(q) / 3
        rvecs = r["cell"][0]

        self.ff.update_rvecs(np.ascontiguousarray(rvecs.T, dtype=np.float64))
        self.ff.update_pos(q.reshape((nat, 3)))
        gpos = np.zeros((nat, 3))
        vtens = np.zeros((3, 3))
        e = self.ff.compute(gpos, vtens)

        r["result"] = [e, -gpos.ravel(), -vtens, ""]
        r["status"] = "Done"
        r["t_finished"] = time.time()


class FFsGDML(ForceField):

    """ A symmetric Gradient Domain Machine Learning (sGDML) force field.
     Chmiela et al. Sci. Adv., 3(5), e1603015, 2017; Nat. Commun., 9(1), 3887, 2018.
     http://sgdml.org/doc/
     https://github.com/stefanch/sGDML
    """

    def __init__(self, latency=1.0, name="", threaded=False, sGDML_model=None, pars=None, dopbc=False):
        """Initialises FFsGDML

        Args:

           sGDML_model: Filename contaning the sGDML model

        """

        # a socket to the communication library is created or linked
        super(FFsGDML, self).__init__(latency, name, pars, dopbc, threaded=threaded)

        # --- Load sGDML package ---
        try:
            from sgdml.predict import GDMLPredict
            from sgdml import __version__
            info(" @ForceField: Using sGDML version " + __version__, verbosity.low)
        except:
            raise ValueError("ERROR: sGDML package not located. Install it via: pip install sgdml")

        # A bit weird to use keyword argument for a required argument, but this
        # is also done in the code above.
        if sGDML_model is None:
            raise ValueError("Must provide a sGDML model file.")

        if dopbc is True:
            raise ValueError("Must set PBCs to False.")

        self.sGDML_model = sGDML_model

        # --- Load sGDML model file. ---
        try:
            self.model = np.load(self.sGDML_model)
            info(" @ForceField: sGDML model " + self.sGDML_model + " loaded" , verbosity.medium)
        except:
            raise ValueError("ERROR: Reading sGDML model " + self.model + " file failed.")

        if "r_unit" in self.model and "e_unit" in self.model:
            info(" @ForceField: The units used in your sGDML model are"\
                 + self.sGDML_model["r_unit"] + " and "+ self.sGDML_model["r_unit"], verbosity.low)

        info(" @ForceField: IMPORTANT: It is always assumed that the units in"\
             + " the provided model file are in Angstroms and kcal/mol.", verbosity.low)

        # --- Constants ---
        self.bohr_to_ang = 1. / UnitMap["length"]['angstrom']
        self.kcalmol_to_hartree = UnitMap["energy"]['cal/mol'] * 1000.
        self.kcalmolang_to_hartreebohr = self.bohr_to_ang * self.kcalmol_to_hartree

        # --- Creates predictor ---
        self.predictor = GDMLPredict(self.model)

        info(" @ForceField: Optimizing parallelization settings for sGDML FF." , verbosity.medium)
        self.predictor.prepare_parallel(n_bulk=1)

    def poll(self):
        """ Polls the forcefield checking if there are requests that should
        be answered, and if necessary evaluates the associated forces and energy. """

        # we have to be thread-safe, as in multi-system mode this might get called by many threads at once
        with self._threadlock:
            for r in self.requests:
                if r["status"] == "Queued":
                    r["status"] = "Running"
                    self.evaluate(r)

    def evaluate(self, r):
        """ Evaluate the energy and forces. """

        E, F = self.predictor.predict(r["pos"] * self.bohr_to_ang)

        r["result"] = [E[0] * self.kcalmol_to_hartree, F.flatten() * self.kcalmolang_to_hartreebohr, np.zeros((3, 3), float), ""]
        r["status"] = "Done"
        r["t_finished"] = time.time()

class FFCavPhSocket(ForceField):

    """
    Socket for dealing with cavity photons interacting with molecules by
    Tao E. Li @ 2020-09-28
    Check https://doi.org/10.1073/pnas.2009272117 for details

    Interface between the PIMD code and a socket for a single replica.

    Deals with an individual replica of the system, obtaining the potential
    force and virial appropriate to this system. Deals with the distribution of
    jobs to the interface.

    Attributes:
        socket: The interface object which contains the socket through which
            communication between the forcefield and the driver is done.
    """

    def __init__(self, latency=1.0, name="", pars=None, dopbc=False,
                 active=np.array([-1]), threaded=True, interface=None):
        """Initialises FFCavPhSocket.

        Args:
           latency: The number of seconds the socket will wait before updating
              the client list.
           name: The name of the forcefield.
           pars: A dictionary used to initialize the forcefield, if required.
              Of the form {'name1': value1, 'name2': value2, ... }.
           dopbc: Decides whether or not to apply the periodic boundary conditions
              before sending the positions to the client code.
           interface: The object used to create the socket used to interact
              with the client codes.
        """

        # a socket to the communication library is created or linked
        super(FFCavPhSocket, self).__init__(latency, name, pars, dopbc, active, threaded)
        if interface is None:
            self.socket = InterfaceCavPhSocket()
        else:
            self.socket = interface
        self.socket.requests = self.requests

    def poll(self):
        """Function to check the status of the client calculations."""

        self.socket.poll()

    def start(self):
        """Spawns a new thread."""

        self.socket.open()
        super(FFCavPhSocket, self).start()

    def stop(self):
        """Closes the socket and the thread."""

        super(FFCavPhSocket, self).stop()
        if self._thread is not None:
            # must wait until loop has ended before closing the socket
            self._thread.join()
        self.socket.close()

class PhotonDriverFabryPerot():

    """
    Photon driver to deal with 2D photonic environment in a Fabry-Perot cavity
    """
    def __init__(self, apply_photon=True, E0=1e-4, omega_c_cminv=3400.0, domega_x_cminv=100.0, 
            domega_y_cminv=100.0, n_mode_x=4, n_mode_y=3, x_grid_1d=np.array([0.1, 0.5, 0.9]), 
            y_grid_1d=np.array([0.1, 0.5]), ph_constraint="none", ph_rep="loose"):

        """
        Initialise PhotonDriverFabryPerot

        In this implementation, the photonic masses are set as 1 a.u.

        Args:
            apply_photon: Determine whether apply light-matter interactions
            E0: varepsilon in the paper, light-matter interactions
            omega_c_cminv: cavity frequency in the perpendicular direction in units of cm-1
            domega_x_cminv: cavity frequency resolution in the in-plane x direction
            domega_y_cminv: cavity frequency resolution in the in-plane y direction
            n_mode_x: number of photon normal modes in the in-plane x direction
            n_mode_y: number of photon normal modes in the in-plane y direction
            x_grid_1d: array of molecular subsystem grid positions in the x direction
            y_grid_1d: array of molecular subsystem grid positions in the y direction
            ph_constraint: string value of possible constraint applied to photons
            ph_rep: loose/dense: if the photon coordinates are stored in 2Nmodes or Nmodes photon "atoms"
        """
        self.hartree_to_cminv = 219474.63
        self.apply_photon = apply_photon
        self.E0 = E0
        self.omega_c = omega_c_cminv / self.hartree_to_cminv
        self.domega_x = domega_x_cminv / self.hartree_to_cminv
        self.domega_y = domega_y_cminv / self.hartree_to_cminv
        self.n_mode_x = n_mode_x
        self.n_mode_y = n_mode_y
        self.x_grid_1d = x_grid_1d # units of Lx
        self.y_grid_1d = y_grid_1d # units of Ly
        self.ph_constraint = ph_constraint

        if self.apply_photon is False:
            self.n_mode_x = 0
            self.n_mode_y = 0

        if self.apply_photon:
            self.ph_rep = ph_rep
            self.init_fabry_perot_geometry(self.ph_constraint, self.ph_rep)
    
    def init_fabry_perot_geometry(self, ph_constraint="none", ph_rep="loose"):

        """
        Initialize the 2D Fabry-Perot geometry and prepare parameters for calculations
        """
        # constraint
        self.kx_coeff = 1.0
        self.ky_coeff = 1.0

        # Apply constraint
        if ph_constraint == "single_mode":
            self.domega_x = 0.0
            self.domega_y = 0.0
            self.n_mode_x = 1
            self.n_mode_y = 1
            self.x_grid_1d = np.array([0.25])
            self.y_grid_1d = np.array([0.25])
        elif ph_constraint == "ky=0":
            self.domega_y = 0.0
            self.n_mode_y = 1
            self.y_grid_1d = np.array([0.])
            self.ky_coeff = 0.0
        elif ph_constraint == "kx=0":
            self.domega_x = 0.0
            self.n_mode_x = 1
            self.x_grid_1d = np.array([0.])
            self.kx_coeff = 0.0
        elif ph_constraint == "kx=ky":
            self.domega_y = 0.0
            self.n_mode_y = 1
            self.y_grid_1d = self.x_grid_1d

        # predefined quantities
        self.n_mode = self.n_mode_x * self.n_mode_y
        if ph_rep == "loose":
            self.n_photon = 2 * self.n_mode
        elif ph_rep == "dense":
            self.n_photon = self.n_mode
        self.n_photon_3 = self.n_photon * 3
        self.pos_ph = np.zeros(self.n_photon_3)

        # generate 2D grid points of molecular bath coords in units of Lx, Ly
        self.x_grid_2d, self.y_grid_2d = np.meshgrid(self.x_grid_1d, self.y_grid_1d)
        self.x_grid_2d = np.reshape(self.x_grid_2d, -1)
        self.y_grid_2d = np.reshape(self.y_grid_2d, -1)
        if ph_constraint == "kx=ky":
            self.x_grid_2d = self.x_grid_1d
            self.y_grid_2d = self.x_grid_1d
        self.n_grid = np.size(self.x_grid_2d)

        # generate 2D grid points of kx, ky in units of 1/Lx, 1/Ly
        self.kx_grid_1d = np.pi * np.array([i+1.0 for i in range(self.n_mode_x)]) * self.kx_coeff
        self.ky_grid_1d = np.pi * np.array([i+1.0 for i in range(self.n_mode_y)]) * self.ky_coeff
        self.kx_grid_2d, self.ky_grid_2d = np.meshgrid(self.kx_grid_1d, self.ky_grid_1d)
        self.kx_grid_2d = np.reshape(self.kx_grid_2d, -1)
        self.ky_grid_2d = np.reshape(self.ky_grid_2d, -1)
        if ph_constraint == "kx=ky":
            self.ky_grid_1d = self.kx_grid_1d
            self.kx_grid_2d = self.kx_grid_1d
            self.ky_grid_2d = self.kx_grid_1d
        #print("kx_grid_1d", self.kx_grid_1d)
        #print("ky_grid_1d", self.ky_grid_1d)
        #print("kx_grid_2d", self.kx_grid_2d)
        #print("ky_grid_2d", self.ky_grid_2d)

        # construct cavity mode frequency array for all photon dimensions
        omega_parallel = np.reshape( ((self.kx_grid_2d / np.pi * self.domega_x)**2 
                         + (self.ky_grid_2d / np.pi * self.domega_y)**2)**0.5, -1)
        print("omega_parallel in cm-1", omega_parallel * self.hartree_to_cminv)
        self.omega_k = (self.omega_c**2 + omega_parallel**2)**0.5
        print("omega_k in cm-1", self.omega_k * self.hartree_to_cminv)
        if ph_rep == "loose":
            self.omega_klambda = np.concatenate((self.omega_k, self.omega_k))
        elif ph_rep == "dense":
            self.omega_klambda = self.omega_k
        #print("omega_klambda", self.omega_klambda)
        self.omega_klambda3 = np.reshape(np.array([[x,x,x] for x in self.omega_klambda]), -1)
        #print("omega_klambda3", self.omega_klambda3)

        # construct varepsilon array for all photon dimensions
        self.varepsilon_k = self.E0 * self.omega_k / np.min(self.omega_k)
        self.varepsilon_klambda = self.E0 * self.omega_klambda / np.min(self.omega_klambda)
        self.varepsilon_klambda3 = self.E0 * self.omega_klambda3 / np.min(self.omega_klambda3)
        #print("varepsilon_klambda", self.varepsilon_klambda)
        #print("varepsilon3", self.varepsilon_klambda3)

        # construct renormalized cavity mode function for each molecular grid point
        self.ftilde_kx = np.zeros((self.n_mode, self.n_grid))
        self.ftilde_ky = np.zeros((self.n_mode, self.n_grid))
        for i in range(self.n_grid):
            x, y = self.x_grid_2d[i], self.y_grid_2d[i]
            self.ftilde_kx[:, i] = 2.0 * np.cos(self.kx_grid_2d * x) * np.sin(self.ky_grid_2d * y)
            self.ftilde_ky[:, i] = 2.0 * np.sin(self.kx_grid_2d * x) * np.cos(self.ky_grid_2d * y)
        self.ftilde_kx3 = np.reshape(np.array([[x, x, x] for x in self.ftilde_kx]), -1)
        self.ftilde_ky3 = np.reshape(np.array([[x, x, x] for x in self.ftilde_ky]), -1)
        print("x_grid_2d (units of Lx, Ly)", self.x_grid_2d)
        print("y_grid_2d (units of Lx, Ly)", self.y_grid_2d)
        #print("kx_grid_2d", self.kx_grid_2d)
        #print("ky_grid_2d", self.ky_grid_2d)
        print("mode function f_kx", self.ftilde_kx)
        print("mode function f_ky", self.ftilde_ky)
         
    def split_atom_ph_coord(self, pos):

        """
        Split atomic and photonic coordinates and update our photonic coordinates

        Args:
            pos: A 3*N position numpy array, [1x, 1y, 1z, 2x, ...]
        
        Returns: 
            Atomic coordinates, Photonic coordinates
        """
        if self.apply_photon:
            pos_at = pos[:-self.n_photon_3]
            pos_ph = pos[-self.n_photon_3:]
            self.pos_ph = pos_ph
        else:
            pos_at = pos
            pos_ph = pos[0:0]
        return pos_at, pos_ph

    def get_ph_energy(self, dx_array, dy_array):
        
        """
        Calculate the total photonic potential energy, including the light-matter
        interaction and dipole self energy

        Args:
            dx_array: x-direction dipole array of molecular subsystems in 2d grid
            dy_array: y-direction dipole array of molecular subsystems in 2d grid
        
        Returns:
            total energy of photonic system
        """
        # calculate the photonic potential energy
        e_ph = np.sum(0.5 * self.omega_klambda3**2 * self.pos_ph**2)
        
        # calculate the dot products between mode functions and dipole array
        d_dot_f_x = np.dot(self.ftilde_kx, dx_array)
        d_dot_f_y = np.dot(self.ftilde_ky, dy_array)

        # calculate the light-matter interaction
        if self.ph_rep == "loose":
            e_int_x = np.sum(self.varepsilon_k * d_dot_f_x * self.pos_ph[:self.n_mode*3:3])
            e_int_y = np.sum(self.varepsilon_k * d_dot_f_y * self.pos_ph[1+self.n_mode*3::3])
        elif self.ph_rep == "dense":
            e_int_x = np.sum(self.varepsilon_k * d_dot_f_x * self.pos_ph[::3])
            e_int_y = np.sum(self.varepsilon_k * d_dot_f_y * self.pos_ph[1::3])

        # calculate the dipole self-energy term
        dse = np.sum((self.varepsilon_k**2 / 2.0 / self.omega_k**2) * (d_dot_f_x**2 + d_dot_f_y**2))

        e_tot = e_ph + e_int_x + e_int_y + dse

        return e_tot

    def get_ph_forces(self, dx_array, dy_array):
        
        """
        Calculate the photonic forces 

        Args:
            dx_array: x-direction dipole array of molecular subsystems in 2d grid
            dy_array: y-direction dipole array of molecular subsystems in 2d grid
        
        Returns:
            force array of all photonic dimensions (3*nphoton) [1x, 1y, 1z, 2x..]
        """
        # calculat the bare photonic contribution of the force
        f_ph = - self.omega_klambda3**2 * self.pos_ph
        # calculate the dot products between mode functions and dipole array
        d_dot_f_x = np.dot(self.ftilde_kx, dx_array)
        d_dot_f_y = np.dot(self.ftilde_ky, dy_array)  
        # calculate the force due to light-matter interactions
        if self.ph_rep == "loose":
            f_ph[:self.n_mode*3:3] -= self.varepsilon_k * d_dot_f_x
            f_ph[self.n_mode*3+1::3] -= self.varepsilon_k * d_dot_f_y
        elif self.ph_rep == "dense":  
            f_ph[::3] -= self.varepsilon_k * d_dot_f_x
            f_ph[1::3] -= self.varepsilon_k * d_dot_f_y   
        return f_ph

    def get_nuc_cav_forces(self, dx_array, dy_array, charge_array_bath):
        
        """
        Calculate the photonic forces on nuclei from MM partial charges

        Args:
            dx_array: x-direction dipole array of molecular subsystems in 2d grid
            dy_array: y-direction dipole array of molecular subsystems in 2d grid
            charge_array_bath: partial charges of all atoms in a single bath
        
        Returns:
            force array of all nuclear dimensions (3*natoms) [1x, 1y, 1z, 2x..]
        """

        # calculate the dot products between mode functions and dipole array
        d_dot_f_x = np.dot(self.ftilde_kx, dx_array)
        d_dot_f_y = np.dot(self.ftilde_ky, dy_array)

        # cavity force on x direction
        if self.ph_rep == "loose":
            Ekx = self.varepsilon_k * self.pos_ph[:self.n_mode*3:3]  
            Eky = self.varepsilon_k * self.pos_ph[self.n_mode*3+1::3]  
        elif self.ph_rep == "dense":
            Ekx = self.varepsilon_k * self.pos_ph[::3]  
            Eky = self.varepsilon_k * self.pos_ph[1::3]  
        Ekx += self.varepsilon_k**2/self.omega_k**2 * d_dot_f_x
        Eky += self.varepsilon_k**2/self.omega_k**2 * d_dot_f_y

        # dimension of independent baths (xy grid points)
        coeff_x = np.dot(np.transpose(Ekx), self.ftilde_kx)
        coeff_y = np.dot(np.transpose(Eky), self.ftilde_ky)
        fx = -np.kron(coeff_x, charge_array_bath)
        fy = -np.kron(coeff_y, charge_array_bath)
        return fx, fy

class FFCavPhFPSocket(ForceField):

    """
    Socket for dealing with cavity photons interacting with molecules by
    Tao E. Li @ 2023-01-15
    Check https://doi.org/10.1073/pnas.2009272117 for details

    Interface between the PIMD code and a socket for a single replica.

    Independent bath approximation will be made to communicate with many sockets

    Deals with an individual replica of the system, obtaining the potential
    force and virial appropriate to this system. Deals with the distribution of
    jobs to the interface.

    Attributes:
        socket: The interface object which contains the socket through which
            communication between the forcefield and the driver is done.
    """

    def __init__(self, latency=1.0, name="", pars=None, dopbc=False,
                 active=np.array([-1]), threaded=True, interface=None,
                 n_independent_bath=1,
                 n_qm_atom=0,
                 mm_charge_array=None,
                 qm_charge_array=None,
                 charge_array=None,
                 apply_photon=True, E0=1e-4, omega_c_cminv=3400.0, domega_x_cminv=100.0, 
                 domega_y_cminv=100.0, n_mode_x=4, n_mode_y=3, x_grid_1d=np.array([0.1, 0.5, 0.9]), 
                 y_grid_1d=np.array([0.1, 0.5]), ph_constraint="none", ph_rep="loose"):

        """Initialises FFCavPhFPSocket.

        Args:
           latency: The number of seconds the socket will wait before updating
              the client list.
           name: The name of the forcefield.
           pars: A dictionary used to initialize the forcefield, if required.
              Of the form {'name1': value1, 'name2': value2, ... }.
           dopbc: Decides whether or not to apply the periodic boundary conditions
              before sending the positions to the client code.
           interface: The object used to create the socket used to interact
              with the client codes.
        """

        # a socket to the communication library is created or linked
        super(FFCavPhFPSocket, self).__init__(latency, name, pars, dopbc, active, threaded)
        if interface is None:
            self.socket = InterfaceSocket()
        else:
            self.socket = interface
        self.socket.requests = self.requests

        # definition of independent baths
        self.n_independent_bath = n_independent_bath
        self.mm_charge_array = mm_charge_array
        self.qm_charge_array = qm_charge_array
        self.charge_array = charge_array
        self.n_qm_atom = n_qm_atom

        # store photonic variables
        self.apply_photon = apply_photon 
        self.E0 = E0
        self.omega_c_cminv = omega_c_cminv
        self.domega_x_cminv = domega_x_cminv
        self.domega_y_cminv = domega_y_cminv
        self.n_mode_x = n_mode_x
        self.n_mode_y = n_mode_y
        self.x_grid_1d = x_grid_1d
        self.y_grid_1d = y_grid_1d
        self.ph_constraint = ph_constraint
        self.ph_rep = ph_rep
        # define the photon environment
        self.ph = PhotonDriverFabryPerot(apply_photon=apply_photon, E0=E0, omega_c_cminv=omega_c_cminv, 
                    domega_x_cminv=domega_x_cminv, domega_y_cminv=domega_y_cminv, n_mode_x=n_mode_x, 
                    n_mode_y=n_mode_y, x_grid_1d=x_grid_1d, y_grid_1d=y_grid_1d, ph_constraint=ph_constraint,
                    ph_rep=ph_rep)

        self._getallcount = 0

    def calc_dipole_xyz_mm(self, pos, n_bath, charge_array_bath):

        """
        Calculate the x, y, and z components of total dipole moment for a single molecular bath (grid point)

        Args:
            pos: position of all atoms (3*n) in all baths
            n_bath: total number of molecular baths (grid points)
            charge_array_bath: charge_array of all atoms (n) in a single bath
        
        Returns: 
            dx_array, dy_array, dz_array: total dipole moment array along x, y, and z directions
        """
        ndim_tot = np.size(pos)
        ndim_local = int(ndim_tot // n_bath)

        dx_array, dy_array, dz_array = [], [], []
        for idx in range(n_bath):
            pos_bath = pos[ndim_local*idx:ndim_local*(idx+1)]
            dx = np.sum(pos_bath[::3] * charge_array_bath)
            dy = np.sum(pos_bath[1::3] * charge_array_bath)
            dz = np.sum(pos_bath[2::3] * charge_array_bath)
            dx_array.append(dx)
            dy_array.append(dy)
            dz_array.append(dz)
        dx_array = np.array(dx_array)
        dy_array = np.array(dy_array)
        dz_array = np.array(dz_array)
        return dx_array, dy_array, dz_array

    def queue(self, atoms, cell, reqid=-1):
        """Adds a request.

        Note that the pars dictionary need to be sent as a string of a
        standard format so that the initialisation of the driver can be done.

        Args:
            atoms: An Atoms object giving the atom positions.
            cell: A Cell object giving the system box.
            pars: An optional dictionary giving the parameters to be sent to the
                driver for initialisation. Defaults to {}.
            reqid: An optional integer that identifies requests of the same type,
               e.g. the bead index

        Returns:
            A list giving the status of the request of the form {'pos': An array
            giving the atom positions folded back into the unit cell,
            'cell': Cell object giving the system box, 'pars': parameter string,
            'result': holds the result as a list once the computation is done,
            'status': a string labelling the status of the calculation,
            'id': the id of the request, usually the bead number, 'start':
            the starting time for the calculation, used to check for timeouts.}.
        """

        par_str = " "

        if not self.pars is None:
            for k, v in list(self.pars.items()):
                par_str += k + " : " + str(v) + " , "
        else:
            par_str = " "

        pbcpos = dstrip(atoms.q).copy()

        # Indexes come from input in a per atom basis and we need to make a per atom-coordinate basis
        # Reformat indexes for full system (default) or piece of system
        # active atoms do not change but we only know how to build this array once we get the positions once
        if self.iactive is None:
            if self.active[0] == -1:
                activehere = np.arange(len(pbcpos))
            else:
                activehere = np.array([[3 * n, 3 * n + 1, 3 * n + 2] for n in self.active])

            # Reassign active indexes in order to use them
            activehere = activehere.flatten()

            # Perform sanity check for active atoms
            if (len(activehere) > len(pbcpos) or activehere[-1] > (len(pbcpos) - 1)):
                raise ValueError("There are more active atoms than atoms!")

            self.iactive = activehere
        
        newreq_lst = []

        # 1. split coordinates to atoms and photons
        pbcpos_atoms, pbcpos_phs = self.ph.split_atom_ph_coord(pbcpos)
        ndim_tot = np.size(pbcpos_atoms)
        ndim_local = int(ndim_tot // self.n_independent_bath)

        # 2. for atomic coordinates, we now evaluate their atomic forces
        for idx in range(self.n_independent_bath):
            pbcpos_local = pbcpos_atoms[ndim_local*idx:ndim_local*(idx+1)].copy()
            iactive_local = self.iactive[0:ndim_local]
            # Let's try to do PBC for the small regions
            if self.dopbc:
                cell.array_pbc(pbcpos_local)
            newreq_local = ForceRequest({
                "id": int(reqid*self.n_independent_bath) + idx,
                "pos": pbcpos_local,
                "active": iactive_local,
                "cell": (dstrip(cell.h).copy(), dstrip(cell.ih).copy()),
                "pars": par_str,
                "result": None,
                "status": "Queued",
                "start": -1,
                "t_queued": time.time(),
                "t_dispatched": 0,
                "t_finished": 0
            })
            newreq_lst.append(newreq_local)

        with self._threadlock:
            for newreq in newreq_lst:
                self.requests.append(newreq)
                self._getallcount += 1

        if not self.threaded:
            self.poll()

        # sleeps until all the new requests have been evaluated
        import sys
        for self.request in newreq_lst:
            while self.request["status"] != "Done":
                if self.request["status"] == "Exit" or softexit.triggered:
                # now, this is tricky. we are stuck here and we cannot return meaningful results.
                # if we return, we may as well output wrong numbers, or mess up things.
                # so we can only call soft-exit and wait until that is done. then kill the thread
                # we are in.
                    softexit.trigger(" @ FORCES : cannot return so will die off here")
                    while softexit.exiting:
                        time.sleep(self.latency)
                    sys.exit()
                time.sleep(self.latency)
            
            """
            with self._threadlock:
                self._getallcount -= 1

            # releases just once, but wait for all requests to be complete
            if self._getallcount == 0:
                self.release(self.request)
                self.request = None
            else:
                while self._getallcount > 0:
                    time.sleep(self.latency)
            """
            self.release(self.request)
            self.request = None

        # ...atomic forces have been calculated at this point
        
        # 3. At this moment, we combine the small requests to a big mega request (update results)
        result_tot = [0.0, np.zeros(len(pbcpos), float), np.zeros((3, 3), float), ""]
        for idx, newreq in enumerate(newreq_lst):
            u, f, vir, extra =  newreq["result"]
            result_tot[0] += u
            result_tot[1][ndim_local*idx:ndim_local*(idx+1)] = f
            result_tot[2] += vir
            result_tot[3] += extra


        if self.ph.apply_photon:
            # 4. calculate total dipole moment array for N baths
            dx_array, dy_array, dz_array = self.calc_dipole_xyz_mm(pos=pbcpos_atoms, n_bath=self.n_independent_bath, charge_array_bath=self.charge_array)
            #info("mux = %.6f muy = %.6f muz = %.6f [units of a.u.]" %(dipole_x_tot, dipole_y_tot, dipole_z_tot), verbosity.medium)
            # 5. calculate photonic contribution of total energy
            e_ph = self.ph.get_ph_energy(dx_array=dx_array, dy_array=dy_array)
            # 6. calculate photonic forces
            f_ph = self.ph.get_ph_forces(dx_array=dx_array, dy_array=dy_array)
            # 7. calculate cavity forces on nuclei
            fx_cav, fy_cav = self.ph.get_nuc_cav_forces(dx_array=dx_array, dy_array=dy_array, charge_array_bath=self.charge_array)
            # 8. add cavity effects to our output
            result_tot[0] += e_ph
            result_tot[1][:ndim_tot:3] += fx_cav
            result_tot[1][1:ndim_tot:3] += fy_cav
            result_tot[1][ndim_tot:] = f_ph
            # additional output for debugging
            """
            print("f_photon", f_ph)
            print("e_photon", e_ph)
            print("fx_nuc_cav", fx_cav[0:10])
            print("fy_nuc_cav", fy_cav[0:10])
            print("fx_cav dimension", fx_cav.size)
            print("fy_cav dimension", fy_cav.size)
            n_local = int(ndim_local // 3)
            print("fx_nuc_cav 2", fx_cav[n_local:n_local+10])
            print("fy_nuc_cav 2", fy_cav[n_local:n_local+10])
            """
        
        # At this moment, we have sucessfully gathered the CavMD forces
        newreq = ForceRequest({
            "id": reqid,
            "pos": pbcpos,
            "active": self.iactive,
            "cell": (dstrip(cell.h).copy(), dstrip(cell.ih).copy()),
            "pars": par_str,
            "result": result_tot,
            "status": newreq_lst[-1]["status"],
            "start": newreq_lst[0]["start"],
            "t_queued": newreq_lst[0]["t_queued"],
            "t_dispatched": newreq_lst[0]["t_dispatched"],
            "t_finished": newreq_lst[-1]["t_finished"]
        })        

        return newreq

    def poll(self):
        """Function to check the status of the client calculations."""

        self.socket.poll()

    def start(self):
        """Spawns a new thread."""

        self.socket.open()
        super(FFCavPhFPSocket, self).start()

    def stop(self):
        """Closes the socket and the thread."""

        super(FFCavPhFPSocket, self).stop()
        if self._thread is not None:
            # must wait until loop has ended before closing the socket
            self._thread.join()
        self.socket.close()

class FFCavPh(ForceField):

    """Full pythonic CavPh interference

    Computes a forcefield necessary for vibrational strong coupling simulation

    Attributes:
       parameters: A dictionary of the parameters used by the driver. Of the
          form {'name': value}.
       requests: During the force calculation step this holds a dictionary
          containing the relevant data for determining the progress of the step.
          Of the form {'atoms': atoms, 'cell': cell, 'pars': parameters,
                       'status': status, 'result': result, 'id': bead id,
                       'start': starting time}.
    """

    def __init__(self, input_xyz_filename="", grad_method='', output_file='', qchem_template='',
                memory_usage="", numpy_memory=2, nthread=1,
                latency=1.0, name="", pars=None, dopbc=False, threaded=False,
                n_independent_bath=1,
                n_qm_atom=-1,
                mm_charge_array=np.array([]),
                qm_charge_array=np.array([])
                ):
        """Initialises FFCavPh.

        Args:
           pars: Optional dictionary, giving the parameters needed by the driver.
           n_itp_dipder: calculate dipder every n_itp_dipder step
        """

        # a socket to the communication library is created or linked
        # NEVER DO PBC -- forces here are computed without.
        super(FFCavPh, self).__init__(latency, name, pars, dopbc=False)

        self.Debye2AU = 1.0 /2.54174623
        self.AU2Angstrom = 1.0 / 1.8897259886
        # Initialize for cavity photon related parts
        self.photons = photons()
        self.apply_photon = self.photons.apply_photon

        # preset parameters
        self.iter = 0

        self.input_xyz_filename = input_xyz_filename
        self.grad_method = grad_method
        self.output_file = output_file
        self.memory_usage = memory_usage
        self.numpy_memory = numpy_memory
        self.nthread = nthread
        self.name = name
        self.qchem_template = qchem_template
        print("Ab initio code will read initial config from", self.input_xyz_filename)
        self.n_independent_bath = n_independent_bath
        if self.n_independent_bath > 1:
            print("### Invoking independent baths approximatin for ab initio calculations ###")
        #print("Theory level", self.grad_method)
        #print("Raw file during ab initio calculation is generated to", self.output_file)
        #print("Memory allocated to ab initio calculation is %s" %self.memory_usage)
        #print("Memory allocated to numpy interface is %d Gb" %self.numpy_memory)
        #print("Number of thread for ab initio calculation is %d" %self.nthread)
        # end of preset parameters

        self.init_nuclear_str = self.initialize_from_xyz(filename=self.input_xyz_filename)

        # load the function of QM/MM part
        self.n_qm_atom = n_qm_atom
        self.mm_charge_array = mm_charge_array
        self.qm_charge_array = qm_charge_array
        self.do_qmmm = False
        if self.n_qm_atom > 0 and self.n_qm_atom < self.nat_idb:
            # check the correctness of input
            print("nat_idb size is", self.nat_idb)
            print("n_qm atom is", self.n_qm_atom)
            if self.mm_charge_array.size + self.n_qm_atom == self.nat_idb:
                print("--- Number of QM atoms plus the MM charges equals to total atoms per bath ---")
                print("--- Will append MM molecules to total dipole moment and dipole derivatives ---")
            else:
                print("By default, setting MM charges as zero when interacting with cavity modes")
                self.mm_charge_array = np.zeros(self.nat_idb - self.n_qm_atom)
            #print(self.mm_charge_array)
            self.do_qmmm = True
            self.n_mm_atom = self.nat_idb - self.n_qm_atom
            print("mm charge array size is", self.mm_charge_array.size)
        # check the validity of QM Charge Array
        if self.qm_charge_array.size > 0:
            print("#! QM atoms assigned with pre-defined partial charges, will not calculate dipole or dipder explicitly")
            print(self.qm_charge_array)
            if self.do_qmmm and self.qm_charge_array.size != self.n_qm_atom:
                softexit.trigger("With QM/MM calculations, wrong size of qm_charge_array")
            if not self.do_qmmm and self.qm_charge_array.size != self.nat_idb:
                softexit.trigger("With pure QM calculations, wrong size of qm_charge_array")
        if self.name == "psi4":
            print("Using %s ab initio force field to do calculation" %name)
            # Initialize psi4 object
            try:
                import psi4
            except:
                raise ImportError("!!!Please install psi4 and psi4numpy interface!!!")
            psi4.set_memory(self.memory_usage)
            numpy_memory = self.numpy_memory
            psi4.set_num_threads(self.nthread)
            psi4.core.set_output_file(self.output_file, False)
            if self.n_independent_bath == 1:
                print(self.init_nuclear_str)
                self.molec = psi4.geometry(self.init_nuclear_str + "\n symmetry c1\n noreorient \n nocom")
            elif self.n_independent_bath > 1:
                print("%d atoms in the original input xyz file %s" %(self.nat, self.input_xyz_filename))
                print("With %d independent baths, each subsystem contains the following %d atoms:" %(self.n_independent_bath, self.nat_idb))
                self.init_nuclear_str_idb = "\n".join(self.init_nuclear_str.split("\n")[:self.nat_idb])
                print(self.init_nuclear_str_idb)
                self.molec = psi4.geometry(self.init_nuclear_str_idb + "\n symmetry c1\n noreorient \n nocom")
        elif self.name == "run_qe_driver.sh" or ("run_qc_driver" in self.name):
            print("Will run the bash script '%s' on the local path..." %self.name)
        elif self.name == "qchem-neo":
            print("Running semiclassical nuclear-electronic orbital (NEO) Ehrenfest dynamics")
            # check if we have a template to create the qchem file
            print("QChem jobs will be run from the following Q-Chem template: %s" %self.qchem_template)
            with open(self.qchem_template, 'r') as f:
                self.qchem_template_content = f.read()
                print(self.qchem_template_content)
        else:
            raise ValueError("%s pythonic force field is unavailable currently" %self.name)

        self.error_flag = False

    def initialize_from_xyz(self, filename):
        if self.apply_photon:
            thefile = open(filename, 'r')
            ntot = int(thefile.readline())
            self.nat = ntot - self.photons.nphoton
            print("Now CavPh force field will deal with %d atoms" %self.nat)
            thefile.readline()
            # Remove several lines for photonic DoFs
            str1 = ""
            for i in range(self.nat):
                str1 += thefile.readline()
            #print("Atomic string is")
            #print(str1)
            # At the same time, I need a list to save the atomic labels
            self.atom_label_lst = str1.split()[::4]
            #print("atomic labels are", self.atom_label_lst)
        else:
            thefile = open(filename, 'r')
            self.nat = int(thefile.readline())
            print("CavPh force field will deal with %d atoms" %self.nat)
            thefile.readline()
            str1 = ""
            for i in range(self.nat):
                str1 += thefile.readline()
            #print("Atomic string is")
            #print(str1)
            # At the same time, I need a list to save the atomic labels
            self.atom_label_lst = str1.split()[::4]
            #print("atomic labels are", self.atom_label_lst)
        self.nat_idb = int(self.nat / self.n_independent_bath)
        return str1

    def construct_qchem_input(self, q, filename):
        q = q.reshape((-1, 3)).copy()
        with open(filename, 'w') as f:
            f.write("$molecule\n0 1\n")
            # write molecular geometry
            for i in range(self.nat):
                f.write("%s  %.10f  %.10f  %.10f\n" %(self.atom_label_lst[i], q[i,0], q[i,1], q[i,2]))
            f.write("$end\n\n")
            f.write(self.qchem_template_content)

    def poll(self):
        """ Polls the forcefield checking if there are requests that should
        be answered, and if necessary evaluates the associated forces and energy. """

        # we have to be thread-safe, as in multi-system mode this might get called by many threads at once
        with self._threadlock:
            for r in self.requests:
                if r["status"] == "Queued":
                    r["status"] = "Running"
                    self.evaluate(r)

    def evaluate(self, r):
        """ Evaluator for FFCavPh"""
        # 1. Obtain the total (nuclear + photonic) position arrary
        q = r["pos"]
        rvecs = r["cell"][0]
        # assuming primitive cubic cell
        rvecs_nparray = np.array(rvecs)
        #self.cell_length = rvecs[0][0]
        self.cell_length = np.array2string(rvecs_nparray)

        if self.apply_photon:
            # 2.1 Separate nuclear and photonic positions
            self.pos_no_photon = q[:-3*self.photons.nphoton]
            self.photons.update_pos(q[-3*self.photons.nphoton:])

            # 2.2 For molecular part, evaluate forces and dipole derivatives
            e, mf, dipole_x_tot, dipole_y_tot, dipole_z_tot, dipder_splitted = self.calc_bare_nuclear_force_dipder(q)
            # if anything wrong occurs in the evaluation, self.error_flag will become true
            # so we try to re-evaluate the forces
            count_retry = 0
            while self.error_flag == True:
                print("Error detected in getting gradients, try to rerun SCF...")
                e, mf, dipole_x_tot, dipole_y_tot, dipole_z_tot, dipder_splitted = self.calc_bare_nuclear_force_dipder(q)
                count_retry += 1
                if (count_retry >= 50):
                    softexit.trigger("Error always detected in obtaining gradients, try to shutdown simulation...")
            info("mux = %.6f muy = %.6f muz = %.6f [units of a.u.]" %(dipole_x_tot, dipole_y_tot, dipole_z_tot), verbosity.medium)

            # 2.3 Evaluate photonic energy (the same as FFCavPhSocket)
            e_photon = self.photons.obtain_potential()
            Ex = self.photons.obtain_Ex()
            Ey = self.photons.obtain_Ey()

            e_int = Ex * dipole_x_tot + Ey * dipole_y_tot
            e += e_photon + e_int + 0.5 * self.photons.coeff_self * (dipole_x_tot**2 + dipole_y_tot**2)

            # 2.4. Modify nuclear force [This part is very different from FFCavPhSocket classical simulation]
            # classical code
            #mf[0::3] += - (Ex + self.photons.coeff_self * dipole_x_tot)  * dmudx
            #mf[1::3] += - (Ey + self.photons.coeff_self * dipole_y_tot)  * dmudy
            # quantum code has cross terms dmu_i/dj (i, j=x,y,z)
            dmuxdx, dmuydx, dmuxdy, dmuydy, dmuxdz, dmuydz = dipder_splitted
            ph_x_coeff = Ex + self.photons.coeff_self * dipole_x_tot
            ph_y_coeff = Ey + self.photons.coeff_self * dipole_y_tot
            mf[0::3] += - ph_x_coeff  * dmuxdx - ph_y_coeff  * dmuydx
            mf[1::3] += - ph_x_coeff  * dmuxdy - ph_y_coeff  * dmuydy
            mf[2::3] += - ph_x_coeff  * dmuxdz - ph_y_coeff  * dmuydz

            # 2.5. Calculate photonic force
            f_photon = self.photons.calc_photon_force(dipole_x_tot, dipole_y_tot)

            # 2.5.1 Update if adding external electric fields on the photonic DoFs
            self.photons.add_pulse(f_photon)
            self.photons.add_cw(f_photon, phase=None)

            # 2.6. Merge the two forces
            mf = np.concatenate((mf[:], f_photon[:]))
        else:
            # 2. Performing conventional energy and force evaluation
            e, mf = self.calc_bare_nuclear_force(q)
            # if anything wrong occurs in the evaluation, self.error_flag will become true
            # so we try to re-evaluate the forces
            count_retry = 0
            while self.error_flag == True:
                print("Error detected in getting gradients, try to rerun SCF...")
                e, mf = self.calc_bare_nuclear_force(q)
                count_retry += 1
                if (count_retry >= 50):
                    softexit.trigger("Error always detected in obtaining gradients, try to shutdown simulation...")

        # 3. Finally, update energy and forces
        self.iter += 1
        r["result"] = [e, mf, np.zeros((3, 3), float), ""]
        r["status"] = "Done"
        r["t_finished"] = time.time()

    def calc_bare_nuclear_force(self, q):
        if self.name == "psi4" and self.n_independent_bath == 1:
            import psi4
            psi4.set_num_threads(self.nthread)
            # update positions for molecules
            self.molec.set_geometry(psi4.core.Matrix.from_array(q.reshape((-1, 3))))
            E, wfn = psi4.energy(self.grad_method, return_wfn=True, molecule=self.molec)
            g, wfn2 = psi4.gradient(self.grad_method, ref_wfn=wfn, molecule=self.molec, return_wfn=True)
            force = -np.asarray(g)
            return E, force.flatten()
        elif self.name == "psi4" and self.n_independent_bath > 1:
            import psi4
            psi4.set_num_threads(self.nthread)
            # update positions for molecules with independent baths approximation
            E_tot = 0.0
            force_lst = []
            #print("- Total atomic coordinate is\n", q)
            for idx_idb in range(self.n_independent_bath):
                q_sub = q[int(idx_idb * self.nat_idb * 3):int((idx_idb+1) * self.nat_idb * 3)]
                #print("--- local q coordinate is\n", q_sub)
                self.molec.set_geometry(psi4.core.Matrix.from_array(q_sub.reshape((-1, 3))))
                E, wfn = psi4.energy(self.grad_method, return_wfn=True, molecule=self.molec)
                force = -np.asarray(psi4.gradient(self.grad_method, ref_wfn=wfn, molecule=self.molec))

                E_tot += E
                force_lst.append(force)
                #print(" --- evaluating No.%d independent bath" %self.n_independent_bath)
                #print(" --- energy is %.7f" %E)
                #print(" --- force is\n", force)

            force = np.array(force_lst).flatten()
            #print("- Final combined energy is %.7f" %E_tot)
            #print("- Final combined force is\n", force)
            return E_tot, force
        elif self.name == "run_qe_driver.sh" and self.n_independent_bath == 1:
            # 1. we construct a string "ATOM1 x y z\n ATOM2 x y z\n ..."
            total_str = ""
            for idx in range(len(self.atom_label_lst)):
                local_str = "%s %.6f %.6f %.6f\n" %(self.atom_label_lst[idx], q[idx*3], q[idx*3+1], q[idx*3+2])
                total_str += local_str
            #print("Use %s to evaluate the following molecular geometry:" %self.name)
            #print(total_str)
            bashCommand = "./" + self.name + ", %s" %total_str + ", %s" %self.cell_length + " ,phonon_no"
            import subprocess
            process = subprocess.Popen(bashCommand.split(","), stdout=subprocess.PIPE)
            output, error = process.communicate()
            # read data from local file
            E = np.loadtxt("IPI_DRIVER_TEMP/energy.ry") * 0.5
            force = np.loadtxt("IPI_DRIVER_TEMP/force.ry_au") * 0.5
            force  = force.flatten()
            return E, force
        elif ("run_qc_driver" in self.name) and self.n_independent_bath >= 1:
            import subprocess
            processes = []
            for idx_idb in range(self.n_independent_bath):
                IPI_DRIVER_TEMP = "IPI_DRIVER_TEMP_%d" %(idx_idb+1)
                # 1. we construct a string "ATOM1 x y z\n ATOM2 x y z\n ..."
                total_str = ""
                idx_start = idx_idb * self.nat_idb
                for idx in range(self.nat_idb):
                    local_str = "%s %.8f %.8f %.8f\n" %(self.atom_label_lst[idx+idx_start], q[(idx+idx_start)*3], q[(idx+idx_start)*3+1], q[(idx+idx_start)*3+2])
                    total_str += local_str
                #print("Use %s to evaluate the following molecular geometry:" %self.name)
                #print(total_str)

                bashCommand = "./" + self.name + ",%s" %total_str + ",%s" %self.cell_length + ",dipder_no" + ",%s" %IPI_DRIVER_TEMP
                if self.do_qmmm:
                    bashCommand += ",qmmm_yes"
                #print("final bash command is")
                #print(bashCommand)
                #print("splitted command is")
                #print(bashCommand.split(","))
                process = subprocess.Popen(bashCommand.split(","), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                processes.append(process)
                #print("processes are", processes)
                try:
                    os.remove("%s/energy.au" %IPI_DRIVER_TEMP)
                    os.remove("%s/egrad.au" %IPI_DRIVER_TEMP)
                except:
                    None

            # run command
            #output = [p.wait() for p in processes]
            output = [p.communicate() for p in processes]
            #print("output is", output)

            E_tot = 0.0
            force_lst = []
            mux_tot, muy_tot, muz_tot = 0.0, 0.0, 0.0
            for idx_idb in range(self.n_independent_bath):
                IPI_DRIVER_TEMP = "IPI_DRIVER_TEMP_%d" %(idx_idb+1)
                # read data from local file
                try:
                    E = np.loadtxt("%s/energy.au" %IPI_DRIVER_TEMP)
                    force = np.loadtxt("%s/egrad.au" %IPI_DRIVER_TEMP)
                    mu_info = np.loadtxt("%s/dipole.debye" %IPI_DRIVER_TEMP)
                except:
                    print("Error occurs when reading files from %s" %IPI_DRIVER_TEMP)
                    self.error_flag = True
                    return 0, 0
                # check if there is anything wrong for this simulation
                try:
                    if force.size == self.nat_idb*3 and E.size == 1 and mu_info.size == 3:
                        self.error_flag = False
                    else:
                        self.error_flag = True
                        return 0, 0
                except:
                    print("Error occurs when evaluating the dimensions of the files")
                    return 0, 0
                    self.error_flag = True
                #print("coordinate is ")
                #print(q)
                if not self.do_qmmm:
                    force  = -1.0 * np.transpose(force).flatten()
                else:
                    force  = -1.0 * force.flatten()
                #print("- Energy is %.10f" %E)
                #print("- Force is")
                #print(force)
                mux, muy, muz = mu_info
                mux *= self.Debye2AU
                muy *= self.Debye2AU
                muz *= self.Debye2AU

                E_tot += E
                force_lst.append(force)
                mux_tot += mux
                muy_tot += muy
                muz_tot += muz

            force = np.array(force_lst).flatten()
            print("mux = %.6f muy = %.6f muz = %.6f [units of a.u.]" %(mux_tot, muy_tot, muz_tot))
            self.error_flag == False
            return E_tot, force
        elif self.name == "qchem-neo" and self.n_independent_bath == 1:
            qchem_input_filename = self.qchem_template + ".in"
            qchem_output_filename = self.qchem_template + ".out"
            self.construct_qchem_input(q * self.AU2Angstrom, qchem_input_filename)
            # run this qchem file
            import subprocess
            bashCommand = ["qchem", "-nt", "%d" %self.nthread, qchem_input_filename]
            process = subprocess.Popen(bashCommand, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            process.communicate()
            # after running this command, get energy and gradients
            try:
                E = np.loadtxt("./current_state.e")
                force = np.loadtxt("./current_state.grad").transpose()
                mue_info = np.loadtxt("./current_state.dipole_e")
                mun_info = np.loadtxt("./current_state.dipole_n")
            except:
                print("Error occurs when reading files")
                self.error_flag = True
                return 0, 0
            force  = -1.0 * force.flatten()
            print("force is", force, "with nthread", self.nthread)
            print("energy is", E)
            print("muex = %.6f muey = %.6f muez = %.6f [units of a.u.]" %(mue_info[0], mue_info[1], mue_info[2]))
            print("munx = %.6f muny = %.6f munz = %.6f [units of a.u.]" %(mun_info[0], mun_info[1], mun_info[2]))
            return E,  force
    def calc_bare_nuclear_force_dipder(self, q):
        if self.name == "psi4" and self.n_independent_bath == 1:
            import psi4
            psi4.set_num_threads(self.nthread)
            # update positions for molecules
            self.molec.set_geometry(psi4.core.Matrix.from_array(q.reshape((-1, 3))))
            E, wfn = psi4.energy(self.grad_method, return_wfn=True, molecule=self.molec)
            # evaluate total dipole moment
            try:
                # depending on the version of psi4
                mux, muy, muz = psi4.core.variable('SCF DIPOLE')
            except:
                mux = psi4.core.variable('SCF DIPOLE X') * self.Debye2AU
                muy = psi4.core.variable('SCF DIPOLE Y') * self.Debye2AU
                muz = psi4.core.variable('SCF DIPOLE Z') * self.Debye2AU
            force = -np.asarray(psi4.gradient(self.grad_method, ref_wfn=wfn, molecule=self.molec))
            if self.qm_charge_array.size > 0:
                dmuxdx = self.qm_charge_array
                dmuydx = np.zeros(self.qm_charge_array.size)
                dmuxdy = np.zeros(self.qm_charge_array.size)
                dmuydy = self.qm_charge_array
                dmuxdz = np.zeros(self.qm_charge_array.size)
                dmuydz = np.zeros(self.qm_charge_array.size)
            else:
                H, wfn2 = psi4.hessian(self.grad_method, return_wfn=True, ref_wfn=wfn)
                dipder = wfn2.variable('SCF DIPOLE GRADIENT').np
                # importantly, I need to split diper array to the desired ones
                dmuxdx = dipder[0::3,0]
                dmuydx = dipder[0::3,1]
                dmuxdy = dipder[1::3,0]
                dmuydy = dipder[1::3,1]
                dmuxdz = dipder[2::3,0]
                dmuydz = dipder[2::3,1]
            dipder_splitted = (dmuxdx, dmuydx, dmuxdy, dmuydy, dmuxdz, dmuydz)

            # check the validity of the output values [be very careful]
            #print("dipole x", mux)
            #print("dipole y", muy)
            #print("original dipder array")
            #print(dipder)
            #print("dmuxdx")
            #print(dmuxdx)
            #print("dmuydx")
            #print(dmuydx)
            #print("dmuxdy")
            #print(dmuxdy)
            #print("dmuydy")
            #print(dmuydy)
            #print("dmuxdz")
            #print(dmuxdz)
            #print("dmuydz")
            #print(dmuydz)

            return E, force.flatten(), mux, muy, muz, dipder_splitted
        elif self.name == "psi4" and self.n_independent_bath > 1:
            import psi4
            psi4.set_num_threads(self.nthread)
            # update positions for molecules with independent baths approximation
            E_tot = 0.0
            force_lst = []
            mux_tot, muy_tot, muz_tot = 0.0, 0.0, 0.0
            dipder_lst = []
            #print("- Total atomic coordinate is\n", q)
            for idx_idb in range(self.n_independent_bath):
                q_sub = q[int(idx_idb * self.nat_idb * 3):int((idx_idb+1) * self.nat_idb * 3)]
                #print("--- local q coordinate is\n", q_sub)
                self.molec.set_geometry(psi4.core.Matrix.from_array(q_sub.reshape((-1, 3))))
                E, wfn = psi4.energy(self.grad_method, return_wfn=True, molecule=self.molec)
                # evaluate total dipole moment
                try:
                    # depending on the version of psi4
                    mux, muy, muz = psi4.core.variable('SCF DIPOLE')
                except:
                    mux = psi4.core.variable('SCF DIPOLE X') * self.Debye2AU
                    muy = psi4.core.variable('SCF DIPOLE Y') * self.Debye2AU
                    muz = psi4.core.variable('SCF DIPOLE Z') * self.Debye2AU
                force = -np.asarray(psi4.gradient(self.grad_method, ref_wfn=wfn, molecule=self.molec))

                if self.qm_charge_array.size > 0:
                    nsize = int(self.qm_charge_array.size * 3)
                    dipder = np.zeros((nsize, 3))
                    dipder[0::3,0] = self.qm_charge_array
                    dipder[1::3,1] = self.qm_charge_array
                    dipder[2::3,2] = self.qm_charge_array
                else:
                    H, wfn2 = psi4.hessian(self.grad_method, return_wfn=True, ref_wfn=wfn)
                    dipder = wfn2.variable('SCF DIPOLE GRADIENT').np
                E_tot += E
                mux_tot += mux
                muy_tot += muy
                muz_tot += muz
                force_lst.append(force)
                dipder_lst.append(dipder)
                #print(" --- evaluating No.%d independent bath" %self.n_independent_bath)
                #print(" --- energy is %.7f" %E)
                #print(" --- force is\n", force)
                #print(" --- dipder is\n", dipder)

            force = np.array(force_lst).flatten()
            dipder = np.array(dipder_lst).reshape(-1, 3)
            #print("- Final combined energy is %.7f" %E_tot)
            #print("- Final combined force is\n", force)
            #print("- Final combined dipder is\n", dipder)
            # importantly, I need to split diper array to the desired ones
            dmuxdx = dipder[0::3,0]
            dmuydx = dipder[0::3,1]
            dmuxdy = dipder[1::3,0]
            dmuydy = dipder[1::3,1]
            dmuxdz = dipder[2::3,0]
            dmuydz = dipder[2::3,1]
            dipder_splitted = (dmuxdx, dmuydx, dmuxdy, dmuydy, dmuxdz, dmuydz)

            return E_tot, force, mux_tot, muy_tot, muz_tot, dipder_splitted
        elif self.name == "run_qe_driver.sh" and self.n_independent_bath == 1:
            # 1. we construct a string "ATOM1 x y z\n ATOM2 x y z\n ..."
            total_str = ""
            for idx in range(len(self.atom_label_lst)):
                local_str = "%s %.6f %.6f %.6f\n" %(self.atom_label_lst[idx], q[idx*3], q[idx*3+1], q[idx*3+2])
                total_str += local_str
            #print("Use %s to evaluate the following molecular geometry:" %self.name)
            #print(total_str)
            bashCommand = "./" + self.name + ", %s" %total_str + ", %s" %self.cell_length + " ,phonon_yes"
            import subprocess
            process = subprocess.Popen(bashCommand.split(","), stdout=subprocess.PIPE)
            output, error = process.communicate()
            # read data from local file
            E = np.loadtxt("IPI_DRIVER_TEMP/energy.ry") * 0.5
            force = np.loadtxt("IPI_DRIVER_TEMP/force.ry_au") * 0.5
            force  = force.flatten()

            mux, muy, muz = np.loadtxt("IPI_DRIVER_TEMP/dipole.au")
            Qx = np.loadtxt("IPI_DRIVER_TEMP/Qx")
            Qy = np.loadtxt("IPI_DRIVER_TEMP/Qy")
            Qz = np.loadtxt("IPI_DRIVER_TEMP/Qz")
            dmuxdx = Qx[:,0]
            dmuydx = Qy[:,0]
            dmuxdy = Qx[:,1]
            dmuydy = Qy[:,1]
            dmuxdz = Qx[:,2]
            dmuydz = Qy[:,2]
            dipder_splitted = (dmuxdx, dmuydx, dmuxdy, dmuydy, dmuxdz, dmuydz)
            return E, force, mux, muy, muz, dipder_splitted
        elif ("run_qc_driver" in self.name) and self.n_independent_bath >= 1:
            import subprocess
            processes = []
            for idx_idb in range(self.n_independent_bath):
                IPI_DRIVER_TEMP = "IPI_DRIVER_TEMP_%d" %(idx_idb+1)
                # 1. we construct a string "ATOM1 x y z\n ATOM2 x y z\n ..."
                total_str = ""
                idx_start = idx_idb * self.nat_idb
                for idx in range(self.nat_idb):
                    local_str = "%s %.8f %.8f %.8f\n" %(self.atom_label_lst[idx+idx_start], q[(idx+idx_start)*3], q[(idx+idx_start)*3+1], q[(idx+idx_start)*3+2])
                    total_str += local_str
                #print("Use %s to evaluate the following molecular geometry:" %self.name)
                #print(total_str)
                bashCommand = "./" + self.name + ", %s" %total_str + ", %s" %self.cell_length + " ,dipder_yes" + ",%s" %IPI_DRIVER_TEMP
                # If qm_charge_array has a size larger than zero, we do not perform expensive simulations
                if self.qm_charge_array.size > 0:
                    #print("qm_charge_array defined, skipping expensive dipder calculation")
                    bashCommand = "./" + self.name + ", %s" %total_str + ", %s" %self.cell_length + " ,dipder_no" + ",%s" %IPI_DRIVER_TEMP
                if self.do_qmmm:
                    bashCommand += ",qmmm_yes"
                process = subprocess.Popen(bashCommand.split(","), stdout=subprocess.PIPE)
                processes.append(process)
                # Finally, try to remove the previous potential files
                try:
                    #os.remove("%s/energy.au" %IPI_DRIVER_TEMP)
                    #os.remove("%s/egrad.au" %IPI_DRIVER_TEMP)
                    #os.remove("%s/dipole.debye" %IPI_DRIVER_TEMP)
                    #os.remove("%s/dipder.au" %IPI_DRIVER_TEMP)
                    os.rename("%s/energy.au" %IPI_DRIVER_TEMP, "%s/energy.au.last_step" %IPI_DRIVER_TEMP)
                    os.rename("%s/egrad.au" %IPI_DRIVER_TEMP, "%s/egrad.au.last_step" %IPI_DRIVER_TEMP)
                    os.rename("%s/dipole.debye" %IPI_DRIVER_TEMP, "%s/dipole.debye.last_step" %IPI_DRIVER_TEMP)
                    os.rename("%s/dipder.au" %IPI_DRIVER_TEMP, "%s/dipder.au.last_step" %IPI_DRIVER_TEMP)
                except:
                    None

            # run command
            output = [p.communicate() for p in processes]

            E_tot = 0.0
            force_lst = []
            mux_tot, muy_tot, muz_tot = 0.0, 0.0, 0.0
            dipder_lst = []
            for idx_idb in range(self.n_independent_bath):
                IPI_DRIVER_TEMP = "IPI_DRIVER_TEMP_%d" %(idx_idb+1)
                # read data from local file
                try:
                    E = np.loadtxt("%s/energy.au" %IPI_DRIVER_TEMP)
                    force = np.loadtxt("%s/egrad.au" %IPI_DRIVER_TEMP)
                    if self.qm_charge_array.size == 0:
                        #print("qm_charge_array not defined, reading dipder from file")
                        mu_info = np.loadtxt("%s/dipole.debye" %IPI_DRIVER_TEMP)
                        dipder = np.loadtxt("%s/dipder.au" %IPI_DRIVER_TEMP)
                    else:
                        #print("qm_charge_array defined, calculating dipder automatically")
                        idx_start = idx_idb * self.nat_idb
                        if self.do_qmmm:
                            q_sub = q[(idx_start)*3 : (idx_start + self.nat_idb)*3]
                            q_qm = q_sub[:self.n_qm_atom*3]
                        else:
                            q_qm = q[idx_start*3:(idx_start + self.nat_idb)*3]
                        mux = np.sum(self.qm_charge_array * q_qm[0::3])
                        muy = np.sum(self.qm_charge_array * q_qm[1::3])
                        muz = np.sum(self.qm_charge_array * q_qm[2::3])
                        mu_info = np.array([mux, muy, muz]) / self.Debye2AU

                        n_dipder = self.nat_idb*9
                        if self.do_qmmm:
                            n_dipder = self.n_qm_atom * 9
                        dipder = np.zeros(n_dipder).reshape(-1, 3)
                        dipder[0::3,0] = self.qm_charge_array
                        dipder[1::3,1] = self.qm_charge_array
                        dipder[2::3,2] = self.qm_charge_array
                        #print("self-calculated dipole vector is", mu_info)
                        #print("self-calculated dipder info is", dipder)
                except:
                    print("Error occurs when reading files from %s" %IPI_DRIVER_TEMP)
                    self.error_flag = True
                    return 0, 0, 0, 0, 0, 0
                # check if there is anything wrong for this simulation
                n_dipder = self.nat_idb*9
                if self.do_qmmm:
                    n_dipder = self.n_qm_atom * 9
                try:
                    if force.size == self.nat_idb*3 and E.size == 1 and dipder.size == n_dipder and mu_info.size == 3:
                        self.error_flag = False
                    else:
                        self.error_flag = True
                        return 0, 0, 0, 0, 0, 0
                except:
                    print("Error occurs when evaluating the dimensions of the files")
                    return 0, 0, 0, 0, 0, 0
                    self.error_flag = True
                #print("coordinate is ")
                #print(q)
                #print("- Before treatment force is")
                #print(force)
                if not self.do_qmmm:
                    force  = -1.0 * np.transpose(force).flatten()
                else:
                    force  = -1.0 * force.flatten()
                #print("- Energy is %.10f" %E)
                #print("- Force is")
                #print(force)
                mux, muy, muz = mu_info
                mux *= self.Debye2AU
                muy *= self.Debye2AU
                muz *= self.Debye2AU
                #print("dipder is")
                #print(dipder)

                if self.do_qmmm:
                    #print("Appending MM partial charges to molecular dipole moment")
                    idx_start = idx_idb * self.nat_idb
                    q_sub = q[(idx_start)*3 : (idx_start + self.nat_idb)*3]
                    #print("molecular coordinate is: ", q_sub)
                    q_sub_mm = q_sub[self.n_qm_atom*3:]
                    #print("MM molecules are:", q_sub_mm)
                    mux_mm = np.sum(q_sub_mm[0::3] * self.mm_charge_array)
                    muy_mm = np.sum(q_sub_mm[1::3] * self.mm_charge_array)
                    muz_mm = np.sum(q_sub_mm[2::3] * self.mm_charge_array)
                    #print("QMsub mux = %.3f, muy = %.3f, muz = %.3f" %(mux, muy, muz))
                    #print("MMsub mux = %.3f, muy = %.3f, muz = %.3f" %(mux_mm, muy_mm, muz_mm))
                    mux += mux_mm
                    muy += muy_mm
                    muz += muz_mm
                    #print("QM+MM mux = %.3f, muy = %.3f, muz = %.3f" %(mux, muy, muz))
                    #print("Appending MM partial charges to molecular dipole derivatives")
                    #print("QMsub dipder = ")
                    #print(dipder)
                    #print("MMsub dipder = ")
                    dipder_mm = np.zeros((self.n_mm_atom*3, 3))
                    dipder_mm[0::3,0] = self.mm_charge_array # dmux/dx
                    dipder_mm[1::3,1] = self.mm_charge_array # dmuy/dy
                    dipder_mm[2::3,2] = self.mm_charge_array # dmuz/dz
                    #print(dipder_mm)
                    #print("QM+MM dipder = ")
                    dipder = np.concatenate((dipder, dipder_mm))
                    #print(dipder)
                    #print("QM+MM forces = ")
                    #print(force)

                E_tot += E
                mux_tot += mux
                muy_tot += muy
                muz_tot += muz
                force_lst.append(force)
                dipder_lst.append(dipder)

            force = np.array(force_lst).flatten()
            dipder = np.array(dipder_lst).reshape(-1, 3)
            #print("The final dipder is")
            #print(dipder)
            # importantly, I need to split diper array to the desired ones
            dmuxdx = dipder[0::3,0]
            dmuydx = dipder[0::3,1]
            dmuxdy = dipder[1::3,0]
            dmuydy = dipder[1::3,1]
            dmuxdz = dipder[2::3,0]
            dmuydz = dipder[2::3,1]
            dipder_splitted = (dmuxdx, dmuydx, dmuxdy, dmuydy, dmuxdz, dmuydz)
            self.error_flag = False
            return E_tot, force, mux_tot, muy_tot, muz_tot, dipder_splitted
