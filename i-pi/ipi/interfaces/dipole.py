"""
Given the configuration of atoms without photon modes, return the total dipole moment
"""

import numpy as np
import os
import json

class dipole:
    def __init__(self):
        self.have_not_set = True
        path = os.getcwd()
        self.run_photon = True
        try:
            with open(path+"/photon_params.json") as json_file:
                data = json.load(json_file)
                print "### Initialise dipole moments from photon_params.json ###"
        except:
            self.run_photon = False
            print "Not found photon_params.json, do conventional nuclear dynamics"
        # Read necessary input from json_file
        # If photon_params.json contains "charge_array" key, it will load it;
        # otherwise we assume that we do H2O simulation with partial charge defined
        # by q-tip4p-f force field
        self.not_have_charge_array = True
        if self.run_photon:
            if data.get("charge_array") is not None:
                print "### Initialise charge array from file ###"
                self.charge_array = np.array(data["charge_array"])
                print self.charge_array
                print "### End of initialise charge array from file ###"
                self.not_have_charge_array = False
            # Find if photon_params.json has defined an incoming pulse at initial times
            self.have_incoming_pulse = data.get("add_pulse", False)
            if self.have_incoming_pulse:
                self.add_pulse_direction = data.get("add_pulse_direction", 0)
                print "## Adding a pulse at %d direction (0-x, 1-y, 2-z) ##" %self.add_pulse_direction
                # pulse_params = ["E0", "tau_FWHM", "omega", "phase", "t0"]
                self.pulse_params = data.get("pulse_params", [1.0, 10.0, 3550.0, 3.14, 10.0])
                self.pulse_params[2] *= 2.998e-5 * 2.0 * np.pi # unit converse from cm-1 to 2pi*fs-1
                # pulse_atoms = [1, 2, 3]
                self.pulse_atoms = np.array(data.get("pulse_atoms", [0, 1, 2]), dtype=np.int32)
                self.pulse_all_atoms = False
                if data.get("pulse_atoms", [0, 1, 2]) == [-1]:
                    self.pulse_all_atoms = True
                print "## excite the following atoms: [-1] means exciting all atoms ##"
                print self.pulse_atoms
                # calculate the corresponding index in the force (correspond to the x axis)
                # atom 1 atom 2 atom 3
                self.pulse_atoms_force_index = np.array([x*3+self.add_pulse_direction for x in self.pulse_atoms])
                self.t = 0.0
                self.dt = data.get("dt", 0.5)
                print "## add initial pulse with E0 %.2E at time %.2f ##" %(self.pulse_params[0], self.pulse_params[4])
            else:
                print "## have not set initial pulse ##"

    def add_pulse(self, mf):
        if self.have_incoming_pulse:
            self.t += self.dt
            if self.t > self.pulse_params[4] and self.t < self.pulse_params[4] + self.pulse_params[1]*8.0:
                self.set_charges()
                t = self.t - self.pulse_params[4] - self.pulse_params[1]*4.0
                Ex = self.pulse_params[0] * np.exp(-t**2 / self.pulse_params[1]**2 \
                    * 2.0 * np.log(2.0)) * np.cos(self.pulse_params[2]*self.t + self.pulse_params[3])
                mf[self.pulse_atoms_force_index] -= Ex * self.charges[self.pulse_atoms]


    def set_charges(self):
        if self.have_not_set:
            self.charges = np.zeros(np.shape(self.pos[:,0]))
            if self.not_have_charge_array:
                self.charges[0::3] = -0.8472
                self.charges[1::3] = 0.4236
                self.charges[2::3] = 0.4236
                print "## Caution, the charge of atoms are set defaultly as follows ##"
                print self.charges
                print "End of Caution"
            else:
                print "## Transfer charge from charge_array from file ##"
                self.charges = self.charge_array
            print "let the charges of each atom as\n", self.charges
            self.have_not_set = False
            # also change the atom array for pulse incoming
            if self.have_incoming_pulse and self.pulse_all_atoms:
                self.pulse_atoms = np.array([n for n in range(np.size(self.pos[:,0]))])
                self.pulse_atoms_force_index = np.array([x*3+self.add_pulse_direction for x in self.pulse_atoms])

    def update_pos(self, pos):
        self.pos = np.reshape(pos, (-1, 3))
        # Boundary condition


    def calc_dipoles_x(self):
        "calculate the x_component of the molecular dipole"
        self.set_charges()
        return self.charges * self.pos[:,0]

    def calc_dipoles_x_tot(self):
        "calculate the total value of the x_component of the molecular dipole"
        self.set_charges()
        return np.sum(self.charges * self.pos[:,0])

    def calc_dipoles_y_tot(self):
        "calculate the total value of the y_component of the molecular dipole"
        self.set_charges()
        return np.sum(self.charges * self.pos[:,1])

    def calc_dipoles_z_tot(self):
        "calculate the total value of the z_component of the molecular dipole"
        self.set_charges()
        return np.sum(self.charges * self.pos[:,2])

    def calc_dipoles_x_which(self, i=0):
        "calculate the total value of the x_component of the molecular dipole"
        self.set_charges()
        return np.sum(self.charges[i*3:i*3+3] * self.pos[i*3:i*3+3,0])

    def calc_dipoles_y_which(self, i=0):
        "calculate the total value of the y_component of the molecular dipole"
        self.set_charges()
        return np.sum(self.charges[i*3:i*3+3] * self.pos[i*3:i*3+3,1])

    def calc_dipoles_z_which(self, i=0):
        "calculate the total value of the z_component of the molecular dipole"
        self.set_charges()
        return np.sum(self.charges[i*3:i*3+3] * self.pos[i*3:i*3+3,2])

    def calc_dipoles_x_which(self, index=0):
        "calculate the total value of the x_component of the molecular dipole"
        self.set_charges()
        return np.sum(self.charges[index*3:index*3+3] * self.pos[index*3:index*3+3, 0])

    def calc_dmudx(self):
        "calculate the spatial derivative of the molecular dipole"
        return self.charges

    def calc_dmudy(self):
        "calculate the spatial derivative of the molecular dipole"
        return self.charges

    def calc_self_dipole_energy(self, pos):
        return self.charges * self.pos[:,0]

if __name__ == "__main__":
    pos = np.random.rand(18)
    print "position is\n", pos
    d = dipole()
