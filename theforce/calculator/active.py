# +
from theforce.regression.gppotential import PosteriorPotential, PosteriorPotentialFromFolder
from theforce.descriptor.atoms import TorchAtoms, AtomsData, LocalsData
from theforce.util.util import date
from ase.calculators.calculator import Calculator, all_changes
from ase.calculators.singlepoint import SinglePointCalculator
import ase
from torch.autograd import grad
import torch
import numpy as np
import warnings


def pad_1d(a, b):
    c = torch.cat([a, torch.zeros(b.size(0)-a.size(0))])
    return c


def pad_2d(a, b):
    c = torch.cat([a, torch.zeros(b.size(0)-a.size(0), a.size(1))], dim=0)
    d = torch.cat([c, torch.zeros(c.size(0), b.size(1)-c.size(1))], dim=1)
    return d


class ActiveCalculator(Calculator):
    implemented_properties = ['energy', 'forces', 'stress']

    def __init__(self, calculator, covariance, process_group=None, ediff=0.1, fdiff=0.1, covdiff=1.,
                 bias=None, logfile='accalc.log', verbose=False, **kwargs):
        """

        calculator:      any ASE calculator
        covariance:      similarity kernel(s) | path to a saved model | model
        process_group:   None | group
        ediff:           energy sensitivity
        fdiff:           forces sensitivity
        covdiff:         covariance-loss sensitivity heuristic
        bias:            scale of the bias potential

        --------------------------------------------------------------------------------------

        At the beginning, covariance is often a list of similarity kernels:
            e.g. theforce.similarity.universal.UniversalSoapKernel(...)
        Later we can use an existing model.
        A trained model can be saved with:  
            e.g. calc.model.to_folder('model/')
        An existing model is loaded with: 
            e.g. ActiveCalculator(calculator, 'model/', ...)

        For parallelism, first call:
            torch.init_process_group('mpi')
        then, set process_group=torch.distributed.group.WORLD in kwargs.

        Sensitivity params can be changed on-the-fly:
            e.g. calc.ediff = 0.05

        If covariance-loss (range [0,1]) for a LCE is greater than covdiff, 
        it will be automaticaly added to the inducing set. 
        Do not make covdiff too small! 
        covdiff = 1 eliminates this heuristic, if one wishes to keep the
        algorithm non-parametric.

        Bias potential derives system away from previous positions.

        """
        Calculator.__init__(self, **kwargs)
        self._calc = calculator
        self.process_group = process_group
        self.get_model(covariance)
        self.ediff = ediff
        self.fdiff = fdiff
        self.covdiff = covdiff
        self.bias = bias
        self.bias_pot = None
        self.verbose = verbose
        self.logfile = logfile
        self.step = 0
        self.log('active calculator says Hello!', mode='w')
        self.normalized = None

    def get_model(self, model):
        if type(model) == str:
            self.model = PosteriorPotentialFromFolder(
                model, group=self.process_group)
        elif type(model) == PosteriorPotential:
            self.model = model
        else:
            self.model = PosteriorPotential(model)

    @property
    def size(self):
        return self.model.ndata, len(self.model.X)

    def calculate(self, _atoms=None, properties=['energy'], system_changes=all_changes):

        if type(_atoms) == ase.atoms.Atoms:
            atoms = TorchAtoms(ase_atoms=_atoms)
            uargs = {'cutoff': self.model.cutoff,
                     'descriptors': self.model.gp.kern.kernels}
            self.to_ase = True
        else:
            atoms = _atoms
            uargs = {}
            self.to_ase = False
        if _atoms is not None and self.process_group is not None:
            atoms.attach_process_group(self.process_group)
        Calculator.calculate(self, atoms, properties, system_changes)
        self.atoms.update(posgrad=True, cellgrad=True,
                          forced=True, dont_save_grads=True, **uargs)

        # build a model
        skip = False
        if self.step == 0:
            if self.model.ndata == 0:
                self.initiate_model()
                skip = True
            self.log('size: {} {}'.format(*self.size))

        # kernel
        self.cov = self.model.gp.kern(self.atoms, self.model.X)

        # energy/forces
        energy = self.calculate_results(True)

        # active learning
        if not skip:
            m, n = self.update()
            if n > 0 or m > 0:  # update results
                energy = self.calculate_results(self.bias is not None)

        # bias potential
        if self.bias is not None:
            bias_energy = self.add_bias()
            log_bias = f'bias: {float(bias_energy)}'
        else:
            log_bias = ''

        # step
        self.log('{} {} {}'.format(float(energy), self.atoms.get_temperature(),
                                   log_bias))
        self.step += 1

    def calculate_results(self, retain_graph=False):
        energy = (self.cov@self.model.mu).sum()
        if self.atoms.is_distributed:
            torch.distributed.all_reduce(energy)
        forces, stress = self.grads(energy, retain_graph=retain_graph)
        self.results['energy'] = energy.detach().numpy()
        self.results['forces'] = forces.detach().numpy()
        self.results['stress'] = stress.flat[[0, 4, 8, 5, 2, 1]]
        return float(energy)

    def add_bias(self):
        if self.bias_pot is None:
            self.bias_pot = torch.zeros(self.cov.size(1))
        mu = (self.model.Mi@self.cov.detach().t()).sum(dim=1)
        if self.atoms.is_distributed:
            torch.distributed.all_reduce(mu)
        self.bias_pot = pad_1d(self.bias_pot, mu) + self.bias*mu
        bias_energy = (self.cov@self.bias_pot).sum()
        torch.distributed.all_reduce(bias_energy)
        bias_forces, bias_stress = self.grads(bias_energy)
        self.results['energy'] += bias_energy.detach().numpy()
        self.results['forces'] += bias_forces.detach().numpy()
        self.results['stress'] += bias_stress.flat[[0, 4, 8, 5, 2, 1]]
        return float(bias_energy)

    def grads(self, energy, retain_graph=False):
        # forces
        rgrad = grad(energy, self.atoms.xyz, retain_graph=True,
                     allow_unused=True)[0]
        forces = torch.zeros_like(self.atoms.xyz) if rgrad is None else -rgrad
        if self.atoms.is_distributed:
            torch.distributed.all_reduce(forces)
        # stress
        stress1 = -(forces[:, None]*self.atoms.xyz[..., None]).sum(dim=0)
        cellgrad, = grad(energy, self.atoms.lll, retain_graph=retain_graph,
                         allow_unused=True)
        if cellgrad is None:
            cellgrad = torch.zeros_like(self.atoms.lll)
        if self.atoms.is_distributed:
            torch.distributed.all_reduce(cellgrad)
        stress2 = (cellgrad[:, None]*self.atoms.lll[..., None]).sum(dim=0)
        try:
            volume = self.atoms.get_volume()
        except ValueError:
            volume = -2  # here stress2=0, thus trace(stress) = virial (?)
        stress = (stress1 + stress2).detach().numpy() / volume
        return forces, stress

    def initiate_model(self):
        atoms = self.snapshot()
        i = atoms.first_of_each_atom_type()
        locs = atoms.gathered()
        inducing = LocalsData([locs[j] for j in i])
        data = AtomsData([atoms])
        self.model.set_data(data, inducing)
        for j in range(atoms.natoms):
            if j not in i:
                self.model.add_1inducing(locs[j], self.ediff)

    def _exact(self, copy):
        tmp = copy.as_ase() if self.to_ase else copy
        tmp.set_calculator(self._calc)
        energy = tmp.get_potential_energy()
        forces = tmp.get_forces()
        if self.rank == 0:
            ase.io.Trajectory('_calc.traj', 'a').write(tmp)
        self.log('exact energy: {}'.format(energy))
        #
        if self.model.ndata > 0:
            dE = self.results['energy'] - energy
            df = abs(self.results['forces'] - forces)
            self.log(
                f'errors:  E_ae: {dE}  F_maxe: {df.max()}  F_mae: {df.mean()}')
        return energy, forces

    def snapshot(self, fake=False, copy=None):
        if copy is None:
            copy = self.atoms.copy()
        if fake:
            energy = self.results['energy']
            forces = self.results['forces']
        else:
            energy, forces = self._exact(copy)
        copy.set_calculator(SinglePointCalculator(copy, energy=energy,
                                                  forces=forces))
        copy.set_targets()
        return copy

    def head(self, energy_and_forces=None):
        added = self.model.data[-1]
        if energy_and_forces is None:
            energy, forces = self._exact(added)
        added.calc.results['energy'] = energy
        added.calc.results['forces'] = forces
        added.set_targets()
        self.model.make_munu()

    def gather(self, x):
        _x = torch.zeros(self.atoms.natoms)
        _x[self.atoms.indices] = x
        torch.distributed.all_reduce(_x)
        return _x

    def get_covloss(self):
        b = self.model.choli@self.cov.T
        c = (b*b).sum(dim=0)
        if not self.normalized:
            alpha = torch.cat([self.model.gp.kern(x, x)
                               for x in self.atoms]).view(-1)
            c = c/alpha
            if self.normalized is None:
                self.normalized = (self.gather(alpha) if self.atoms.is_distributed
                                   else alpha).allclose(torch.ones([]))
                self.log(f'kernel normalization status {self.normalized}')
        beta = (1 - c).clamp(min=0.).sqrt()
        if self.atoms.is_distributed:
            beta = self.gather(beta)
        return beta

    def update_inducing(self):
        added_beta = 0
        added_diff = 0
        added_indices = []
        self.blind = False
        while True:
            if len(added_indices) == self.atoms.natoms:
                break
            beta = self.get_covloss()
            q = torch.argsort(beta, descending=True)
            for k in q.tolist():
                if k not in added_indices:
                    break
            if beta[k].isclose(torch.ones([])):
                self.blind = True
            loc = self.atoms.local(k, detach=True)
            if loc.number in self.model.gp.species:
                if beta[k] > self.covdiff:
                    self.model.add_inducing(loc)
                    added_beta += 1
                    x = self.model.gp.kern(self.atoms, loc)
                    self.cov = torch.cat([self.cov, x], dim=1)
                    added_indices.append(k)
                else:
                    _ediff = (self.ediff if len(self.model.X) > 1
                              else torch.finfo().tiny)
                    added, delta = self.model.add_1inducing(
                        loc, _ediff, detach=False)
                    if added:
                        added_diff += 1
                        x = self.model.gp.kern(self.atoms, loc)
                        self.cov = torch.cat([self.cov, x], dim=1)
                        added_indices.append(k)
                    else:
                        break
        added = added_beta + added_diff
        if added > 0:
            details = [(k, self.atoms.numbers[k]) for k in added_indices]
            self.log('added indu: {} ({},{})-> size: {} {} details: {}'.format(
                added, added_beta, added_diff, *self.size, details))
            if self.blind:
                self.log('model may be blind! trying robust algorithm.')
        return added

    def update_data(self, try_fake=True):
        n = self.model.ndata
        new = self.snapshot(fake=try_fake)
        self.model.add_1atoms(new, self.ediff, self.fdiff)
        added = self.model.ndata - n
        if added > 0:
            if try_fake:
                self.head()
            self.log('added data: {} -> size: {} {}'.format(
                added, *self.size))
        return added

    def update(self):
        m = self.update_inducing()
        n = self.update_data(try_fake=not self.blind) if m > 0 else 0
        # tunning noise is unstable!
        # if n > 0 and not self.model.is_well():
        #    self.log(f'tuning noise: {self.model.gp.noise.signal} ->')
        #    self.model.tune_noise(min_steps=10, verbose=self.verbose)
        #    self.log(f'noise: {self.model.gp.noise.signal}')
        return m, n

    @property
    def rank(self):
        if torch.distributed.is_initialized():
            return torch.distributed.get_rank()
        else:
            return 0

    def log(self, mssge, mode='a'):
        if self.rank == 0:
            with open(self.logfile, mode) as f:
                f.write('{} {} {}\n'.format(date(), self.step, mssge))
                if self.verbose:
                    print('{} {} {}'.format(date(), self.step, mssge))


def parse_logfile(file='accalc.log'):
    energies = []
    temperatures = []
    exact_energies = []
    errors = []
    for line in open(file):
        split = line.split()[2:]

        try:
            step = int(split[0])
        except IndexError:
            continue

        try:
            energies += [(step, float(split[1]))]
            temperatures += [(step, float(split[2]))]
        except:
            pass

        if 'exact energy' in line:
            exact_energies += [(step, float(split[3]))]

        if 'errors' in line:
            errors += [(step, [float(v) for v in split[3:8:2]])]
    return energies, exact_energies, errors
