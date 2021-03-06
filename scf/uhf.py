#!/usr/bin/env python


import time
from functools import reduce
import numpy
import scipy.linalg
from pyscf import lib
from pyscf.lib import logger
from pyscf.scf import hf
from pyscf.scf import _vhf
import pyscf.scf.chkfile


def init_guess_by_minao(mol):
    '''Generate initial guess density matrix based on ANO basis, then project
    the density matrix to the basis set defined by ``mol``

    Returns:
        Density matrices, a list of 2D ndarrays for alpha and beta spins
    '''
    dm = hf.init_guess_by_minao(mol)
    return numpy.array((dm*.5,dm*.5))

def init_guess_by_1e(mol):
    dm = hf.init_guess_by_1e(mol)
    return numpy.array((dm*.5,dm*.5))

def init_guess_by_atom(mol):
    dm = hf.init_guess_by_atom(mol)
    return numpy.array((dm*.5,dm*.5))

def init_guess_by_chkfile(mol, chkfile_name, project=True):
    from pyscf.scf import addons
    chk_mol, scf_rec = pyscf.scf.chkfile.load_scf(chkfile_name)

    def fproj(mo):
        if project:
            return addons.project_mo_nr2nr(chk_mol, mo, mol)
        else:
            return mo
    if scf_rec['mo_coeff'].ndim == 2:
        mo = scf_rec['mo_coeff']
        mo_occ = scf_rec['mo_occ']
        if numpy.iscomplexobj(mo):
            raise NotImplementedError('TODO: project DHF orbital to UHF orbital')
        mo_coeff = fproj(mo)
        mo_a = mo_coeff[:,mo_occ>0]
        mo_b = mo_coeff[:,mo_occ>1]
        dm_a = numpy.dot(mo_a, mo_a.T)
        dm_b = numpy.dot(mo_b, mo_b.T)
        dm = numpy.array((dm_a, dm_b))
    else: #UHF
        mo = scf_rec['mo_coeff']
        mo_occ = scf_rec['mo_occ']
        dm = make_rdm1([fproj(mo[0]),fproj(mo[1])], mo_occ)
    return dm

def get_init_guess(mol, key='minao'):
    return UHF(mol).get_init_guess(mol, key)

def make_rdm1(mo_coeff, mo_occ):
    '''One-particle density matrix

    Returns:
        A list of 2D ndarrays for alpha and beta spins
    '''
    mo_a = mo_coeff[0]
    mo_b = mo_coeff[1]
    dm_a = numpy.dot(mo_a*mo_occ[0], mo_a.T.conj())
    dm_b = numpy.dot(mo_b*mo_occ[1], mo_b.T.conj())
    return numpy.array((dm_a,dm_b))

def get_veff(mol, dm, dm_last=0, vhf_last=0, hermi=1, vhfopt=None):
    r'''Unrestricted Hartree-Fock potential matrix of alpha and beta spins,
    for the given density matrix

    .. math::

        V_{ij}^\alpha &= \sum_{kl} (ij|kl)(\gamma_{lk}^\alpha+\gamma_{lk}^\beta)
                       - \sum_{kl} (il|kj)\gamma_{lk}^\alpha \\
        V_{ij}^\beta  &= \sum_{kl} (ij|kl)(\gamma_{lk}^\alpha+\gamma_{lk}^\beta)
                       - \sum_{kl} (il|kj)\gamma_{lk}^\beta

    Args:
        mol : an instance of :class:`Mole`

        dm : a list of ndarrays
            A list of density matrices, stored as (alpha,alpha,...,beta,beta,...)

    Kwargs:
        dm_last : ndarray or a list of ndarrays or 0
            The density matrix baseline.  When it is not 0, this function computes
            the increment of HF potential w.r.t. the reference HF potential matrix.
        vhf_last : ndarray or a list of ndarrays or 0
            The reference HF potential matrix.
        hermi : int
            Whether J, K matrix is hermitian

            | 0 : no hermitian or symmetric
            | 1 : hermitian
            | 2 : anti-hermitian

        vhfopt :
            A class which holds precomputed quantities to optimize the
            computation of J, K matrices

    Returns:
        :math:`V_{hf} = (V^\alpha, V^\beta)`.  :math:`V^\alpha` (and :math:`V^\beta`)
        can be a list matrices, corresponding to the input density matrices.

    Examples:

    >>> import numpy
    >>> from pyscf import gto, scf
    >>> from pyscf.scf import _vhf
    >>> mol = gto.M(atom='H 0 0 0; H 0 0 1.1')
    >>> dmsa = numpy.random.random((3,mol.nao_nr(),mol.nao_nr()))
    >>> dmsb = numpy.random.random((3,mol.nao_nr(),mol.nao_nr()))
    >>> dms = numpy.vstack((dmsa,dmsb))
    >>> dms.shape
    (6, 2, 2)
    >>> vhfa, vhfb = scf.uhf.get_veff(mol, dms, hermi=0)
    >>> vhfa.shape
    (3, 2, 2)
    >>> vhfb.shape
    (3, 2, 2)
    '''
    dm = numpy.asarray(dm)
    nao = dm.shape[-1]
    ddm = dm - numpy.asarray(dm_last)
    # dm.reshape(-1,nao,nao) to remove first dim, compress (dma,dmb)
    vj, vk = hf.get_jk(mol, ddm.reshape(-1,nao,nao), hermi=hermi, vhfopt=vhfopt)
    vhf = _makevhf(vj.reshape(dm.shape), vk.reshape(dm.shape))
    vhf += numpy.asarray(vhf_last)
    return vhf

def get_fock(mf, h1e, s1e, vhf, dm, cycle=-1, adiis=None,
             diis_start_cycle=None, level_shift_factor=None, damp_factor=None):
    if diis_start_cycle is None:
        diis_start_cycle = mf.diis_start_cycle
    if level_shift_factor is None:
        level_shift_factor = mf.level_shift
    if damp_factor is None:
        damp_factor = mf.damp

    if isinstance(level_shift_factor, (tuple, list, numpy.ndarray)):
        shifta, shiftb = level_shift_factor
    else:
        shifta = shiftb = level_shift_factor
    if isinstance(damp_factor, (tuple, list, numpy.ndarray)):
        dampa, dampb = damp_factor
    else:
        dampa = dampb = damp_factor

    f = h1e + vhf
    if f.ndim == 2:
        f = (f, f)
    if isinstance(dm, numpy.ndarray) and dm.ndim == 2:
        dm = [dm*.5] * 2
    if 0 <= cycle < diis_start_cycle-1 and abs(dampa)+abs(dampb) > 1e-4:
        f = (hf.damping(s1e, dm[0], f[0], dampa),
             hf.damping(s1e, dm[1], f[1], dampb))
    if adiis and cycle >= diis_start_cycle:
        f = adiis.update(s1e, dm, f)
    if abs(shifta)+abs(shiftb) > 1e-4:
        f = (hf.level_shift(s1e, dm[0], f[0], shifta),
             hf.level_shift(s1e, dm[1], f[1], shiftb))
    return numpy.array(f)

def get_occ(mf, mo_energy=None, mo_coeff=None):
    if mo_energy is None: mo_energy = mf.mo_energy
    e_idx_a = numpy.argsort(mo_energy[0])
    e_idx_b = numpy.argsort(mo_energy[1])
    e_sort_a = mo_energy[0][e_idx_a]
    e_sort_b = mo_energy[1][e_idx_b]
    nmo = mo_energy[0].size
    n_a, n_b = mf.nelec
    mo_occ = numpy.zeros_like(mo_energy)
    mo_occ[0][e_idx_a[:n_a]] = 1
    mo_occ[1][e_idx_b[:n_b]] = 1
    if mf.verbose >= logger.INFO and n_a < nmo and n_b > 0 and n_b < nmo:
        if e_sort_a[n_a-1]+1e-3 > e_sort_a[n_a]:
            logger.warn(mf, '!! alpha nocc = %d  HOMO %.15g >= LUMO %.15g',
                        n_a, e_sort_a[n_a-1], e_sort_a[n_a])
        else:
            logger.info(mf, '  alpha nocc = %d  HOMO = %.15g  LUMO = %.15g',
                        n_a, e_sort_a[n_a-1], e_sort_a[n_a])

        if e_sort_b[n_b-1]+1e-3 > e_sort_b[n_b]:
            logger.warn(mf, '!! beta  nocc = %d  HOMO %.15g >= LUMO %.15g',
                        n_b, e_sort_b[n_b-1], e_sort_b[n_b])
        else:
            logger.info(mf, '  beta  nocc = %d  HOMO = %.15g  LUMO = %.15g',
                        n_b, e_sort_b[n_b-1], e_sort_b[n_b])

        if e_sort_a[n_a-1]+1e-3 > e_sort_b[n_b]:
            logger.warn(mf, '!! system HOMO %.15g >= system LUMO %.15g',
                        e_sort_b[n_a-1], e_sort_b[n_b])

        numpy.set_printoptions(threshold=nmo)
        logger.debug(mf, '  alpha mo_energy =\n%s', mo_energy[0])
        logger.debug(mf, '  beta  mo_energy =\n%s', mo_energy[1])
        numpy.set_printoptions(threshold=1000)

    if mo_coeff is not None and mf.verbose >= logger.DEBUG:
        ss, s = mf.spin_square((mo_coeff[0][:,mo_occ[0]>0],
                                  mo_coeff[1][:,mo_occ[1]>0]),
                                  mf.get_ovlp())
        logger.debug(mf, 'multiplicity <S^2> = %.8g  2S+1 = %.8g', ss, s)
    return mo_occ

def get_grad(mo_coeff, mo_occ, fock_ao):
    '''UHF Gradients'''
    occidxa = mo_occ[0] > 0
    occidxb = mo_occ[1] > 0
    viridxa = ~occidxa
    viridxb = ~occidxb

    ga = reduce(numpy.dot, (mo_coeff[0][:,viridxa].T.conj(), fock_ao[0],
                            mo_coeff[0][:,occidxa]))
    gb = reduce(numpy.dot, (mo_coeff[1][:,viridxb].T.conj(), fock_ao[1],
                            mo_coeff[1][:,occidxb]))
    return numpy.hstack((ga.ravel(), gb.ravel()))

def energy_elec(mf, dm=None, h1e=None, vhf=None):
    '''Electronic energy of Unrestricted Hartree-Fock

    Returns:
        Hartree-Fock electronic energy and the 2-electron part contribution
    '''
    if dm is None: dm = mf.make_rdm1()
    if h1e is None:
        h1e = mf.get_hcore()
    if isinstance(dm, numpy.ndarray) and dm.ndim == 2:
        dm = numpy.array((dm*.5, dm*.5))
    if vhf is None:
        vhf = mf.get_veff(mf.mol, dm)
    e1 = numpy.einsum('ij,ij', h1e.conj(), dm[0]+dm[1])
    e_coul =(numpy.einsum('ij,ji', vhf[0], dm[0]) +
             numpy.einsum('ij,ji', vhf[1], dm[1])) * .5
    return e1+e_coul, e_coul

# mo_a and mo_b are occupied orbitals
def spin_square(mo, s=1):
    r'''Spin of the given UHF orbitals

    .. math::

        S^2 = \frac{1}{2}(S_+ S_-  +  S_- S_+) + S_z^2

    where :math:`S_+ = \sum_i S_{i+}` is effective for all beta occupied
    orbitals; :math:`S_- = \sum_i S_{i-}` is effective for all alpha occupied
    orbitals.

    1. There are two possibilities for :math:`S_+ S_-`
        1) same electron :math:`S_+ S_- = \sum_i s_{i+} s_{i-}`,

        .. math::

            \sum_i \langle UHF|s_{i+} s_{i-}|UHF\rangle
             = \sum_{pq}\langle p|s_+s_-|q\rangle \gamma_{qp} = n_\alpha

        2) different electrons :math:`S_+ S_- = \sum s_{i+} s_{j-},  (i\neq j)`.
        There are in total :math:`n(n-1)` terms.  As a two-particle operator,

        .. math::

            \langle S_+ S_- \rangle = \langle ij|s_+ s_-|ij\rangle
                                    - \langle ij|s_+ s_-|ji\rangle
                                    = -\langle i^\alpha|j^\beta\rangle
                                       \langle j^\beta|i^\alpha\rangle

    2. Similarly, for :math:`S_- S_+`
        1) same electron

        .. math::

           \sum_i \langle s_{i-} s_{i+}\rangle = n_\beta

        2) different electrons

        .. math::

            \langle S_- S_+ \rangle = -\langle i^\beta|j^\alpha\rangle
                                       \langle j^\alpha|i^\beta\rangle

    3. For :math:`S_z^2`
        1) same electron

        .. math::

            \langle s_z^2\rangle = \frac{1}{4}(n_\alpha + n_\beta)

        2) different electrons

        .. math::

            &\frac{1}{2}\sum_{ij}(\langle ij|2s_{z1}s_{z2}|ij\rangle
                                 -\langle ij|2s_{z1}s_{z2}|ji\rangle) \\
            &=\frac{1}{4}(\langle i^\alpha|i^\alpha\rangle \langle j^\alpha|j^\alpha\rangle
             - \langle i^\alpha|i^\alpha\rangle \langle j^\beta|j^\beta\rangle
             - \langle i^\beta|i^\beta\rangle \langle j^\alpha|j^\alpha\rangle
             + \langle i^\beta|i^\beta\rangle \langle j^\beta|j^\beta\rangle) \\
            &-\frac{1}{4}(\langle i^\alpha|i^\alpha\rangle \langle i^\alpha|i^\alpha\rangle
             + \langle i^\beta|i^\beta\rangle\langle i^\beta|i^\beta\rangle) \\
            &=\frac{1}{4}(n_\alpha^2 - n_\alpha n_\beta - n_\beta n_\alpha + n_\beta^2)
             -\frac{1}{4}(n_\alpha + n_\beta) \\
            &=\frac{1}{4}((n_\alpha-n_\beta)^2 - (n_\alpha+n_\beta))

    In total

    .. math::

        \langle S^2\rangle &= \frac{1}{2}
        (n_\alpha-\sum_{ij}\langle i^\alpha|j^\beta\rangle \langle j^\beta|i^\alpha\rangle
        +n_\beta -\sum_{ij}\langle i^\beta|j^\alpha\rangle\langle j^\alpha|i^\beta\rangle)
        + \frac{1}{4}(n_\alpha-n_\beta)^2 \\

    Args:
        mo : a list of 2 ndarrays
            Occupied alpha and occupied beta orbitals

    Kwargs:
        s : ndarray
            AO overlap

    Returns:
        A list of two floats.  The first is the expectation value of S^2.
        The second is the corresponding 2S+1

    Examples:

    >>> mol = gto.M(atom='O 0 0 0; H 0 0 1; H 0 1 0', basis='ccpvdz', charge=1, spin=1, verbose=0)
    >>> mf = scf.UHF(mol)
    >>> mf.kernel()
    -75.623975516256706
    >>> mo = (mf.mo_coeff[0][:,mf.mo_occ[0]>0], mf.mo_coeff[1][:,mf.mo_occ[1]>0])
    >>> print('S^2 = %.7f, 2S+1 = %.7f' % spin_square(mo, mol.intor('cint1e_ovlp_sph')))
    S^2 = 0.7570150, 2S+1 = 2.0070027
    '''
    mo_a, mo_b = mo
    nocc_a = mo_a.shape[1]
    nocc_b = mo_b.shape[1]
    s = reduce(numpy.dot, (mo_a.T, s, mo_b))
    ssxy = (nocc_a+nocc_b) * .5 - (s**2).sum()
    ssz = (nocc_b-nocc_a)**2 * .25
    ss = ssxy + ssz
    s = numpy.sqrt(ss+.25) - .5
    return ss, s*2+1

def analyze(mf, verbose=logger.DEBUG, **kwargs):
    '''Analyze the given SCF object:  print orbital energies, occupancies;
    print orbital coefficients; Mulliken population analysis; Dipole moment
    '''
    from pyscf.lo import orth
    from pyscf.tools import dump_mat
    mo_energy = mf.mo_energy
    mo_occ = mf.mo_occ
    mo_coeff = mf.mo_coeff
    if isinstance(verbose, logger.Logger):
        log = verbose
    else:
        log = logger.Logger(mf.stdout, verbose)
    ss, s = mf.spin_square((mo_coeff[0][:,mo_occ[0]>0],
                            mo_coeff[1][:,mo_occ[1]>0]), mf.get_ovlp())
    log.note('multiplicity <S^2> = %.8g  2S+1 = %.8g', ss, s)

    log.note('**** MO energy ****')
    log.note('                             alpha | beta                alpha | beta')
    for i in range(mo_occ.shape[1]):
        log.note('MO #%-3d energy= %-18.15g | %-18.15g occ= %g | %g',
                 i+1, mo_energy[0][i], mo_energy[1][i],
                 mo_occ[0][i], mo_occ[1][i])
    ovlp_ao = mf.get_ovlp()
    if verbose >= logger.DEBUG:
        log.debug(' ** MO coefficients (expansion on meta-Lowdin AOs) for alpha spin **')
        label = mf.mol.spheric_labels(True)
        orth_coeff = orth.orth_ao(mf.mol, 'meta_lowdin', s=ovlp_ao)
        c_inv = numpy.dot(orth_coeff.T, ovlp_ao)
        dump_mat.dump_rec(mf.stdout, c_inv.dot(mo_coeff[0]), label, start=1,
                          **kwargs)
        log.debug(' ** MO coefficients (expansion on meta-Lowdin AOs) for beta spin **')
        dump_mat.dump_rec(mf.stdout, c_inv.dot(mo_coeff[1]), label, start=1,
                          **kwargs)

    dm = mf.make_rdm1(mo_coeff, mo_occ)
    return (mf.mulliken_meta(mf.mol, dm, s=ovlp_ao, verbose=log),
            mf.dip_moment(mf.mol, dm, verbose=log))

def mulliken_pop(mol, dm, s=None, verbose=logger.DEBUG):
    '''Mulliken population analysis
    '''
    if s is None:
        s = hf.get_ovlp(mol)
    if isinstance(verbose, logger.Logger):
        log = verbose
    else:
        log = logger.Logger(mol.stdout, verbose)
    if isinstance(dm, numpy.ndarray) and dm.ndim == 2:
        dm = numpy.array((dm*.5, dm*.5))
    pop_a = numpy.einsum('ij->i', dm[0]*s)
    pop_b = numpy.einsum('ij->i', dm[1]*s)
    label = mol.spheric_labels(False)

    log.note(' ** Mulliken pop       alpha | beta **')
    for i, s in enumerate(label):
        log.note('pop of  %s %10.5f | %-10.5f',
                 '%d%s %s%4s'%s, pop_a[i], pop_b[i])

    log.note(' ** Mulliken atomic charges  **')
    chg = numpy.zeros(mol.natm)
    for i, s in enumerate(label):
        chg[s[0]] += pop_a[i] + pop_b[i]
    for ia in range(mol.natm):
        symb = mol.atom_symbol(ia)
        chg[ia] = mol.atom_charge(ia) - chg[ia]
        log.note('charge of  %d%s =   %10.5f', ia, symb, chg[ia])
    return (pop_a,pop_b), chg

def mulliken_meta(mol, dm_ao, verbose=logger.DEBUG, pre_orth_method='ANO',
                  s=None):
    '''Mulliken population analysis, based on meta-Lowdin AOs.
    '''
    from pyscf.lo import orth
    if s is None:
        s = hf.get_ovlp(mol)
    if isinstance(verbose, logger.Logger):
        log = verbose
    else:
        log = logger.Logger(mol.stdout, verbose)
    if isinstance(dm_ao, numpy.ndarray) and dm_ao.ndim == 2:
        dm_ao = numpy.array((dm_ao*.5, dm_ao*.5))
    c = orth.pre_orth_ao(mol, pre_orth_method)
    orth_coeff = orth.orth_ao(mol, 'meta_lowdin', pre_orth_ao=c, s=s)
    c_inv = numpy.dot(orth_coeff.T, s)
    dm_a = reduce(numpy.dot, (c_inv, dm_ao[0], c_inv.T.conj()))
    dm_b = reduce(numpy.dot, (c_inv, dm_ao[1], c_inv.T.conj()))

    log.note(' ** Mulliken pop alpha/beta on meta-lowdin orthogonal AOs **')
    return mulliken_pop(mol, (dm_a,dm_b), numpy.eye(orth_coeff.shape[0]), log)
mulliken_pop_meta_lowdin_ao = mulliken_meta

def map_rhf_to_uhf(mf):
    '''Create UHF object based on the RHF object'''
    from pyscf.scf import addons
    return addons.convert_to_uhf(mf)

def canonicalize(mf, mo_coeff, mo_occ, fock=None):
    '''Canonicalization diagonalizes the UHF Fock matrix within occupied,
    virtual subspaces separatedly (without change occupancy).
    '''
    mo_occ = numpy.asarray(mo_occ)
    assert(mo_occ.ndim == 2)
    if fock is None:
        dm = mf.make_rdm1(mo_coeff, mo_occ)
        fock = mf.get_hcore() + mf.get_jk(mol, dm)
    occidxa = mo_occ[0] == 1
    occidxb = mo_occ[1] == 1
    viridxa = mo_occ[0] == 0
    viridxb = mo_occ[1] == 0
    def eig_(fock, mo_coeff, idx, es, cs):
        if numpy.count_nonzero(idx) > 0:
            orb = mo_coeff[:,idx]
            f1 = reduce(numpy.dot, (orb.T.conj(), fock, orb))
            e, c = scipy.linalg.eigh(f1)
            es[idx] = e
            cs[:,idx] = numpy.dot(mo_coeff[:,idx], c)
    mo = numpy.empty_like(mo_coeff)
    mo_e = numpy.empty(mo_occ.shape)
    eig_(fock[0], mo_coeff[0], occidxa, mo_e[0], mo[0])
    eig_(fock[0], mo_coeff[0], viridxa, mo_e[0], mo[0])
    eig_(fock[1], mo_coeff[1], occidxb, mo_e[1], mo[1])
    eig_(fock[1], mo_coeff[1], viridxb, mo_e[1], mo[1])
    return mo_e, mo

def det_ovlp(mo1, mo2, occ1, occ2, ovlp):
    r''' Calculate the overlap between two different determinants. It is the product
    of single values of molecular orbital overlap matrix.

    .. math::

        S_{12} = \langle \Psi_A | \Psi_B \rangle
        = (\mathrm{det}\mathbf{U}) (\mathrm{det}\mathbf{V^\dagger})\prod\limits_{i=1}\limits^{2N} \lambda_{ii}

    where :math:`\mathbf{U}, \mathbf{V}, \lambda` are unitary matrices and single
    values generated by single value decomposition(SVD) of the overlap matrix
    :math:`\mathbf{O}` which is the overlap matrix of two sets of molecular orbitals:

    .. math::

        \mathbf{U}^\dagger \mathbf{O} \mathbf{V} = \mathbf{\Lambda}

    Args:
        mo1, mo2 : 2D ndarrays
            Molecualr orbital coefficients
        occ1, occ2: 2D ndarrays
            occupation numbers

    Return:
        A list: the product of single values: float
            x_a, x_b: 1D ndarrays
            :math:`\mathbf{U} \mathbf{\Lambda}^{-1} \mathbf{V}^\dagger`
            They are used to calculate asymmetric density matrix
    '''

    if numpy.sum(occ1) != numpy.sum(occ2):
        raise RuntimeError('Electron numbers are not equal. Electronic coupling does not exist.')

    c1_a = mo1[0][:, occ1[0]>0]
    c1_b = mo1[1][:, occ1[1]>0]
    c2_a = mo2[0][:, occ2[0]>0]
    c2_b = mo2[1][:, occ2[1]>0]
    o_a = reduce(numpy.dot, (c1_a.T, ovlp, c2_a))
    o_b = reduce(numpy.dot, (c1_b.T, ovlp, c2_b))
    u_a, s_a, vt_a = numpy.linalg.svd(o_a)
    u_b, s_b, vt_b = numpy.linalg.svd(o_b)
    x_a = reduce(numpy.dot, (u_a, numpy.diag(numpy.reciprocal(s_a)), vt_a))
    x_b = reduce(numpy.dot, (u_b, numpy.diag(numpy.reciprocal(s_b)), vt_b))
    return numpy.prod(s_a)*numpy.prod(s_b), numpy.array((x_a, x_b))

def make_asym_dm(mo1, mo2, occ1, occ2, x):
    r'''One-particle asymmetric density matrix

    Args:
        mo1, mo2 : 2D ndarrays
            Molecualr orbital coefficients
        occ1, occ2: 2D ndarrays
            Occupation numbers
        x: 2D ndarrays
            :math:`\mathbf{U} \mathbf{\Lambda}^{-1} \mathbf{V}^\dagger`.
            See also :func:`det_ovlp`

    Return:
        A list of 2D ndarrays for alpha and beta spin

    Examples:

    >>> mf1 = scf.UHF(gto.M(atom='H 0 0 0; F 0 0 1.3', basis='ccpvdz')).run()
    >>> mf2 = scf.UHF(gto.M(atom='H 0 0 0; F 0 0 1.4', basis='ccpvdz')).run()
    >>> s = gto.intor_cross('cint1e_ovlp_sph', mf1.mol, mf2.mol)
    >>> det, x = det_ovlp(mf1.mo_coeff, mf1.mo_occ, mf2.mo_coeff, mf2.mo_occ, s)
    >>> adm = make_asym_dm(mf1.mo_coeff, mf1.mo_occ, mf2.mo_coeff, mf2.mo_occ, x)
    >>> adm.shape
    (2, 19, 19)
    '''

    mo1_a = mo1[0][:, occ1[0]>0]
    mo1_b = mo1[1][:, occ1[1]>0]
    mo2_a = mo2[0][:, occ2[0]>0]
    mo2_b = mo2[1][:, occ2[1]>0]
    dm_a = reduce(numpy.dot, (mo1_a, x[0], mo2_a.T.conj()))
    dm_b = reduce(numpy.dot, (mo1_b, x[1], mo2_b.T.conj()))
    return numpy.array((dm_a, dm_b))

def dip_moment(mol, dm, unit_symbol='Debye', verbose=logger.NOTE):
    r''' Dipole moment calculation

    .. math::

        \mu_x = -\sum_{\mu}\sum_{\nu} P_{\mu\nu}(\nu|x|\mu) + \sum_A Q_A X_A\\
        \mu_y = -\sum_{\mu}\sum_{\nu} P_{\mu\nu}(\nu|y|\mu) + \sum_A Q_A Y_A\\
        \mu_z = -\sum_{\mu}\sum_{\nu} P_{\mu\nu}(\nu|z|\mu) + \sum_A Q_A Z_A

    where :math:`\mu_x, \mu_y, \mu_z` are the x, y and z components of dipole
    moment

    Args:
         mol: an instance of :class:`Mole`

         dm : a list of 2D ndarrays
              a list of density matrices

    Return:
        A list: the dipole moment on x, y and z component
    '''
    if isinstance(dm, numpy.ndarray) and dm.ndim == 2:
        return hf.dip_moment(mol, dm, unit_symbol, verbose)
    else:
        return hf.dip_moment(mol, dm[0]+dm[1], unit_symbol, verbose)

class UHF(hf.SCF):
    __doc__ = hf.SCF.__doc__ + '''
    Attributes for UHF:
        nelec : (int, int)
            If given, freeze the number of (alpha,beta) electrons to the given value.
        level_shift : number or two-element list
            level shift (in Eh) for alpha and beta Fock if two-element list is given.

    Examples:

    >>> mol = gto.M(atom='O 0 0 0; H 0 0 1; H 0 1 0', basis='ccpvdz', charge=1, spin=1, verbose=0)
    >>> mf = scf.UHF(mol)
    >>> mf.kernel()
    -75.623975516256706
    >>> print('S^2 = %.7f, 2S+1 = %.7f' % mf.spin_square())
    S^2 = 0.7570150, 2S+1 = 2.0070027
    '''
    def __init__(self, mol):
        hf.SCF.__init__(self, mol)
        # self.mo_coeff => [mo_a, mo_b]
        # self.mo_occ => [mo_occ_a, mo_occ_b]
        # self.mo_energy => [mo_energy_a, mo_energy_b]

        n_b = (mol.nelectron - mol.spin) // 2
        self.nelec = (mol.nelectron-n_b, n_b)
        self._keys = self._keys.union(['nelec'])

    def dump_flags(self):
        if hasattr(self, 'nelectron_alpha'):
            logger.warn(self, 'Note the API updates: attribute nelectron_alpha was replaced by attribute nelec')
            #raise RuntimeError('API updates')
            self.nelec = (self.nelectron_alpha,
                          self.mol.nelectron-self.nelectron_alpha)
            delattr(self, 'nelectron_alpha')
        hf.SCF.dump_flags(self)
        logger.info(self, 'number electrons alpha = %d  beta = %d', *self.nelec)

    def eig(self, fock, s):
        e_a, c_a = hf.SCF.eig(self, fock[0], s)
        e_b, c_b = hf.SCF.eig(self, fock[1], s)
        return lib.asarray((e_a,e_b)), lib.asarray((c_a,c_b))

    get_fock = get_fock

    get_occ = get_occ

    def get_grad(self, mo_coeff, mo_occ, fock=None):
        if fock is None:
            dm1 = self.make_rdm1(mo_coeff, mo_occ)
            fock = self.get_hcore(self.mol) + self.get_veff(self.mol, dm1)
        return get_grad(mo_coeff, mo_occ, fock)

    @lib.with_doc(make_rdm1.__doc__)
    def make_rdm1(self, mo_coeff=None, mo_occ=None):
        if mo_coeff is None:
            mo_coeff = self.mo_coeff
        if mo_occ is None:
            mo_occ = self.mo_occ
        return make_rdm1(mo_coeff, mo_occ)

    energy_elec = energy_elec

    def init_guess_by_minao(self, mol=None):
        '''Initial guess in terms of the overlap to minimal basis.'''
        dm = hf.SCF.init_guess_by_minao(self, mol)
        return numpy.array([dm*.5]*2)

    def init_guess_by_atom(self, mol=None):
        dm = hf.SCF.init_guess_by_atom(self, mol)
        return numpy.array([dm*.5]*2)

    def init_guess_by_1e(self, mol=None):
        if mol is None: mol = self.mol
        logger.info(self, 'Initial guess from hcore.')
        h1e = self.get_hcore(mol)
        s1e = self.get_ovlp(mol)
        mo_energy, mo_coeff = self.eig((h1e,h1e), s1e)
        mo_occ = self.get_occ(mo_energy, mo_coeff)
        return self.make_rdm1(mo_coeff, mo_occ)

    def init_guess_by_chkfile(self, chkfile=None, project=True):
        if chkfile is None: chkfile = self.chkfile
        return init_guess_by_chkfile(self.mol, chkfile, project=project)

    def get_jk(self, mol=None, dm=None, hermi=1):
        '''Coulomb (J) and exchange (K)

        Args:
            dm : a list of 2D arrays or a list of 3D arrays
                (alpha_dm, beta_dm) or (alpha_dms, beta_dms)
        '''
        if mol is None: mol = self.mol
        if dm is None: dm = self.make_rdm1()
        dm = numpy.asarray(dm)
        nao = dm.shape[-1]  # Get nao from dm shape because the hamiltonian
                            # might be not defined from mol
        if self._eri is not None or mol.incore_anyway or self._is_mem_enough():
            if self._eri is None:
                self._eri = _vhf.int2e_sph(mol._atm, mol._bas, mol._env)
            vj, vk = hf.dot_eri_dm(self._eri, dm.reshape(-1,nao,nao), hermi)
        else:
            vj, vk = hf.SCF.get_jk(self, mol, dm.reshape(-1,nao,nao), hermi)
        return vj.reshape(dm.shape), vk.reshape(dm.shape)

    @lib.with_doc(get_veff.__doc__)
    def get_veff(self, mol=None, dm=None, dm_last=0, vhf_last=0, hermi=1):
        if mol is None: mol = self.mol
        if dm is None: dm = self.make_rdm1()
        dm = numpy.asarray(dm)
        if dm.ndim == 2:
            dm = numpy.asarray((dm*.5,dm*.5))
        if (self._eri is not None or not self.direct_scf or
            mol.incore_anyway or self._is_mem_enough()):
            vj, vk = self.get_jk(mol, dm, hermi)
            vhf = _makevhf(vj, vk)
        else:
            ddm = dm - numpy.asarray(dm_last)
            vj, vk = self.get_jk(mol, ddm, hermi)
            vhf = _makevhf(vj, vk) + numpy.asarray(vhf_last)
        return vhf

    def analyze(self, verbose=None, **kwargs):
        if verbose is None: verbose = self.verbose
        return analyze(self, verbose, **kwargs)

    def mulliken_pop(self, mol=None, dm=None, s=None, verbose=logger.DEBUG):
        if mol is None: mol = self.mol
        if dm is None: dm = self.make_rdm1()
        if s is None: s = self.get_ovlp(mol)
        return mulliken_pop(mol, dm, s=s, verbose=verbose)

    def mulliken_meta(self, mol=None, dm=None, verbose=logger.DEBUG,
                      pre_orth_method='ANO', s=None):
        if mol is None: mol = self.mol
        if dm is None: dm = self.make_rdm1()
        return mulliken_meta(mol, dm, s=s, verbose=verbose,
                             pre_orth_method=pre_orth_method)

    @lib.with_doc(spin_square.__doc__)
    def spin_square(self, mo_coeff=None, s=None):
        if mo_coeff is None:
            mo_coeff = (self.mo_coeff[0][:,self.mo_occ[0]>0],
                        self.mo_coeff[1][:,self.mo_occ[1]>0])
        if s is None:
            s = self.get_ovlp()
        return spin_square(mo_coeff, s)

    canonicalize = canonicalize

    @lib.with_doc(det_ovlp.__doc__)
    def det_ovlp(self, mo1, mo2, occ1, occ2, ovlp=None):
        if ovlp is None: ovlp = self.get_ovlp()
        return det_ovlp(mo1, mo2, occ1, occ2, ovlp)

    @lib.with_doc(make_asym_dm.__doc__)
    def make_asym_dm(self, mo1, mo2, occ1, occ2, x):
        return make_asym_dm(mo1, mo2, occ1, occ2, x)

    @lib.with_doc(dip_moment.__doc__)
    def dip_moment(self, mol=None, dm=None, unit_symbol=None, verbose=logger.NOTE):
        if mol is None: mol = self.mol
        if dm is None: dm =self.make_rdm1()
        if unit_symbol is None: unit_symbol='Debye'
        return dip_moment(mol, dm, unit_symbol, verbose=verbose)

def _makevhf(vj, vk):
    vj = vj[0] + vj[1]
    v_a = vj - vk[0]
    v_b = vj - vk[1]
    return pyscf.lib.asarray((v_a,v_b))
