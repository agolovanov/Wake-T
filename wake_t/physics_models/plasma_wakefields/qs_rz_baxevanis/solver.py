"""
This module implements the methods for calculating the plasma wakefields
using the 2D r-z reduced model from P. Baxevanis and G. Stupakov.

See https://journals.aps.org/prab/abstract/10.1103/PhysRevAccelBeams.21.071301
for the full details about this model.
"""

import numpy as np
import scipy.constants as ct
from numba import njit
import aptools.plasma_accel.general_equations as ge

from wake_t.particles.deposition import deposit_3d_distribution
from .deposition import deposit_plasma_particles
from wake_t.particles.interpolation import gather_sources_qs_baxevanis
from wake_t.utilities.other import radial_gradient
from .plasma_push.rk4 import evolve_plasma_rk4
from .plasma_push.ab5 import evolve_plasma_ab5
from .psi_and_derivatives import (
    calculate_psi, calculate_psi_and_derivatives_at_particles)
from .b_theta import calculate_b_theta, calculate_b_theta_at_particles
from .plasma_particles import PlasmaParticles


def calculate_wakefields(laser_a2, beam_part, r_max, xi_min, xi_max,
                         n_r, n_xi, ppc, n_p, r_max_plasma=None,
                         parabolic_coefficient=0., p_shape='cubic',
                         max_gamma=10., plasma_pusher='rk4'):
    """
    Calculate the plasma wakefields generated by the given laser pulse and
    electron beam in the specified grid points.

    Parameters:
    -----------
    laser_a2 : ndarray
        A (nz x nr) array containing the square of the laser envelope.

    beam_part : list
        List of numpy arrays containing the spatial coordinates and charge of
        all beam particles, i.e [x, y, xi, q].

    r_max : float
        Maximum radial position up to which plasma wakefield will be
        calculated.

    xi_min : float
        Minimum longitudinal (speed of light frame) position up to which
        plasma wakefield will be calculated.

    xi_max : float
        Maximum longitudinal (speed of light frame) position up to which
        plasma wakefield will be calculated.

    n_r : int
        Number of grid elements along r in which to calculate the wakefields.

    n_xi : int
        Number of grid elements along xi in which to calculate the wakefields.

    ppc : int (optional)
        Number of plasma particles per 1d cell along the radial direction.

    n_p : float
        Plasma density in units of m^{-3}.

    r_max_plasma : float
        Maximum radial extension of the plasma column. If `None`, the plasma
        extends up to the `r_max` boundary of the simulation box.

    parabolic_coefficient : float
        The coefficient for the transverse parabolic density profile. The
        radial density distribution is calculated as
        `n_r = n_p * (1 + parabolic_coefficient * r**2)`, where `n_p` is the
        local on-axis plasma density.

    p_shape : str
        Particle shape to be used for the beam charge deposition. Possible
        values are 'linear' or 'cubic'.

    max_gamma : float
        Plasma particles whose `gamma` exceeds `max_gamma` are considered to
        violate the quasistatic condition and are put at rest (i.e.,
        `gamma=1.`, `pr=pz=0.`).

    plasma_pusher : str
        Numerical pusher for the plasma particles. Possible values are `'rk4'`
        and `'ab5'`.

    """
    s_d = ge.plasma_skin_depth(n_p * 1e-6)
    r_max = r_max / s_d
    xi_min = xi_min / s_d
    xi_max = xi_max / s_d
    dr = r_max / n_r
    dxi = (xi_max - xi_min) / (n_xi - 1)
    parabolic_coefficient = parabolic_coefficient * s_d**2

    # Maximum radial extent of the plasma.
    if r_max_plasma is None:
        r_max_plasma = r_max
    else:
        r_max_plasma = r_max_plasma / s_d

    # Initialize plasma particles.
    pp = PlasmaParticles(
        r_max, r_max_plasma, parabolic_coefficient, dr, ppc, plasma_pusher)
    pp.initialize()
    (a2_pp, nabla_a2_pp, b_theta_0_pp, b_theta_pp,
     psi_pp, dr_psi_pp, dxi_psi_pp) = pp.get_field_arrays()

    # Calculate and allocate laser quantities, including guard cells.
    a2_rz = np.zeros((n_xi+4, n_r+4))
    nabla_a2_rz = np.zeros((n_xi+4, n_r+4))
    a2_rz[2:-2, 2:-2] = laser_a2
    nabla_a2_rz[2:-2, 2:-2] = radial_gradient(laser_a2, dr)

    # Initialize field arrays, including guard cells.
    rho = np.zeros((n_xi+4, n_r+4))
    chi = np.zeros((n_xi+4, n_r+4))
    psi = np.zeros((n_xi+4, n_r+4))
    W_r = np.zeros((n_xi+4, n_r+4))
    E_z = np.zeros((n_xi+4, n_r+4))
    b_theta_bar = np.zeros((n_xi+4, n_r+4))

    # Field node coordinates.
    r_fld = np.linspace(dr / 2, r_max - dr / 2, n_r)
    xi_fld = np.linspace(xi_min, xi_max, n_xi)

    # Beam source. This code is needed while no proper support particle
    # beams as input is implemented.
    b_theta_0_mesh = calculate_beam_source_from_particles(
        *beam_part, n_p, n_r, n_xi, r_fld[0], xi_fld[0], dr, dxi, p_shape)

    # Main loop.
    for step in np.arange(n_xi):
        i = -1 - step
        xi = xi_fld[i]

        # Gather source terms at position of plasma particles.
        gather_sources_qs_baxevanis(
            a2_rz, nabla_a2_rz, b_theta_0_mesh, xi_fld[0], xi_fld[-1],
            r_fld[0], r_fld[-1], dxi, dr, pp.r, xi, a2_pp, nabla_a2_pp,
            b_theta_0_pp)

        # Get sorted particle indices
        idx = np.argsort(pp.r)

        # Calculate wakefield potential and derivatives at plasma particles.
        calculate_psi_and_derivatives_at_particles(
            pp.r, pp.pr, pp.q, idx, pp.r_max_plasma, pp.dr_p,
            pp.parabolic_coefficient, psi_pp, dr_psi_pp, dxi_psi_pp)

        # Update gamma and pz of plasma particles
        update_gamma_and_pz(pp.gamma, pp.pz, pp.pr, a2_pp, psi_pp)

        # Calculate azimuthal magnetic field from the plasma at the location of
        # the plasma particles.
        calculate_b_theta_at_particles(
            pp.r, pp.pr, pp.q, pp.gamma, psi_pp, dr_psi_pp, dxi_psi_pp,
            b_theta_0_pp, nabla_a2_pp, idx, pp.dr_p, b_theta_pp)

        # If particles violate the quasistatic condition, slow them down again.
        # This preserves the charge and shows better behavior than directly
        # removing them.
        idx_keep = np.where(pp.gamma >= max_gamma)
        if idx_keep[0].size > 0:
            pp.pz[idx_keep] = 0.
            pp.gamma[idx_keep] = 1.
            pp.pr[idx_keep] = 0.

        # Calculate fields at specified radii for current plasma column.
        psi[i-2, 2:-2] = calculate_psi(
            r_fld, pp.r, pp.q, idx, pp.r_max_plasma, pp.parabolic_coefficient)
        b_theta_bar[i-2, 2:-2] = calculate_b_theta(
            r_fld, pp.r, pp.pr, pp.q, pp.gamma, psi_pp, dr_psi_pp, dxi_psi_pp,
            b_theta_0_pp, nabla_a2_pp, idx)

        # Deposit rho and chi of plasma column
        w_rho = pp.q / (dr * pp.r * (1 - pp.pz/pp.gamma))
        w_chi = w_rho / pp.gamma
        deposit_plasma_particles(xi, pp.r, w_rho, xi_min, r_fld[0], n_xi, n_r,
                                 dxi, dr, rho, p_shape=p_shape)
        deposit_plasma_particles(xi, pp.r, w_chi, xi_min, r_fld[0], n_xi, n_r,
                                 dxi, dr, chi, p_shape=p_shape)

        if step < n_xi-1:
            # Evolve plasma to next xi step.
            if plasma_pusher == 'ab5':
                evolve_plasma_ab5(pp, dxi)
            elif plasma_pusher == 'rk4':
                evolve_plasma_rk4(
                    pp, dxi, xi, a2_rz, nabla_a2_rz, b_theta_0_mesh,
                    r_fld, xi_fld)
            else:
                raise ValueError(
                    "Plasma pusher '{}' not recognized.".format(plasma_pusher))

    # Calculate derived fields (E_z, W_r, and E_r).
    dxi_psi, dr_psi = np.gradient(psi[2:-2, 2:-2], dxi, dr, edge_order=2)
    E_z[2:-2, 2:-2] = -dxi_psi
    W_r[2:-2, 2:-2] = -dr_psi
    B_theta = b_theta_bar + b_theta_0_mesh
    E_r = W_r + B_theta
    return rho, chi, E_r, E_z, B_theta, xi_fld, r_fld


@njit
def update_gamma_and_pz(gamma, pz, pr, a2, psi):
    """
    Update the gamma factor and longitudinal momentum of the plasma particles.

    Parameters:
    -----------
    gamma, pz : ndarray
        Arrays containing the current gamma factor and longitudinal momentum
        of the plasma particles (will be modified here).

    pr, a2, psi : ndarray
        Arrays containing the radial momentum of the particles and the
        value of a2 and psi at the position of the particles.

    """
    for i in range(pr.shape[0]):
        gamma[i] = (1 + pr[i]**2 + a2[i] + (1+psi[i])**2) / (2 * (1+psi[i]))
        pz[i] = (1 + pr[i]**2 + a2[i] - (1+psi[i])**2) / (2 * (1+psi[i]))


def calculate_beam_source_from_particles(
        x, y, xi, q, n_p, n_r, n_xi, r_min, xi_min, dr, dxi, p_shape):
    """
    Return a (nz+4, nr+4) array with the azimuthal magnetic field
    from a particle distribution. This is Eq. (18) in the original paper.

    """
    # Plasma skin depth.
    s_d = ge.plasma_skin_depth(n_p / 1e6)

    # Get and normalize particle coordinate arrays.
    xi_n = xi / s_d
    x_n = x / s_d
    y_n = y / s_d

    # Calculate particle weights.
    w = - q / ct.e / (2 * np.pi * dr * dxi * s_d ** 3 * n_p)

    # Obtain charge distribution (using cubic particle shape by default).
    q_dist = np.zeros((n_xi + 4, n_r + 4))
    deposit_3d_distribution(xi_n, x_n, y_n, w, xi_min, r_min, n_xi, n_r, dxi,
                            dr, q_dist, p_shape=p_shape, use_ruyten=True)

    # Remove guard cells.
    q_dist = q_dist[2:-2, 2:-2]

    # Allovate magnetic field array.
    b_theta = np.zeros((n_xi+4, n_r+4))

    # Radial position of grid points.
    r_grid_g = (0.5 + np.arange(n_r)) * dr

    # At each grid cell, calculate integral only until cell center by
    # assuming that half the charge is evenly distributed within the cell
    # (i.e., substract half the charge)
    subs = q_dist / 2

    # At the first grid point along r, subtstact an additonal 1/4 of the
    # charge. This comes from assuming that the density has to be zero on axis.
    subs[:, 0] += q_dist[:, 0]/4

    # Calculate field by integration.
    b_theta[2:-2, 2:-2] = (
        (np.cumsum(q_dist, axis=1) - subs) * dr / np.abs(r_grid_g))

    return b_theta
