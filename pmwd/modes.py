from functools import partial

from jax import jit, checkpoint, custom_vjp
from jax import random
import jax.numpy as jnp

from pmwd.boltzmann import linear_power, linear_transfer
from pmwd.pm_util import fftfreq, fftfwd, fftinv


#TODO follow pmesh to fill the modes in Fourier space
@partial(jit, static_argnames=('real', 'unit_abs'))
def white_noise(seed, conf, real=False, unit_abs=False):
    """White noise Fourier or real modes.

    Parameters
    ----------
    seed : int
        Seed for the pseudo-random number generator.
    conf : Configuration
    real : bool, optional
        Whether to return real or Fourier modes.
    unit_abs : bool, optional
        Whether to set the absolute values to 1.

    Returns
    -------
    modes : jax.Array of conf.float_dtype
        White noise Fourier or real modes, both dimensionless with zero mean and unit
        variance.

    """
    key = random.PRNGKey(seed)

    # sample linear modes on Lagrangian particle grid
    modes = random.normal(key, shape=conf.ptcl_grid_shape, dtype=conf.float_dtype)

    if real and not unit_abs:
        return modes

    modes = fftfwd(modes, norm='ortho')

    if unit_abs:
        modes /= jnp.abs(modes)

    if real:
        modes = fftinv(modes, shape=conf.ptcl_grid_shape, norm='ortho')

    return modes


@custom_vjp
def _safe_sqrt(x):
    return jnp.sqrt(x)

def _safe_sqrt_fwd(x):
    y = _safe_sqrt(x)
    return y, y

def _safe_sqrt_bwd(y, y_cot):
    x_cot = jnp.where(y != 0, 0.5 / y * y_cot, 0)
    return (x_cot,)

_safe_sqrt.defvjp(_safe_sqrt_fwd, _safe_sqrt_bwd)


@partial(jit, static_argnums=4)
# @partial(checkpoint, static_argnums=4)
def linear_modes(modes, cosmo, conf, a=None, real=False):
    """Linear matter overdensity Fourier or real modes.

    Parameters
    ----------
    modes : jax.Array
        Fourier or real modes with white noise prior.
    cosmo : Cosmology
    conf : Configuration
    a : float or None, optional
        Scale factors. Default (None) is to not scale the output modes by growth.
    real : bool, optional
        Whether to return real or Fourier modes.

    Returns
    -------
    modes : jax.Array of conf.float_dtype
        Linear matter overdensity Fourier or real modes, in [L^3] or dimensionless,
        respectively.

    Notes
    -----

    .. math::

        \delta(\mathbf{k}) = \sqrt{V P_\mathrm{lin}(k)} \omega(\mathbf{k})

    """
    kvec = fftfreq(conf.ptcl_grid_shape, conf.ptcl_spacing, dtype=conf.float_dtype)
    k = jnp.sqrt(sum(k**2 for k in kvec))

    if a is not None:
        a = jnp.asarray(a, dtype=conf.float_dtype)

    if jnp.isrealobj(modes):
        modes = fftfwd(modes, norm='ortho')
    
    if cosmo.f_nl_loc_ is not None:
        Tlin = linear_transfer(k, a, cosmo, conf)*k*k
        Pprim = 2*jnp.pi**2. * cosmo.A_s * (k/cosmo.k_pivot)**(cosmo.n_s-1.)\
                    * k**(-3.)
        Pprim = Pprim.at[0,0,0].set(0.)
        
        modes *= _safe_sqrt(Pprim / conf.ptcl_cell_vol)
        
        modes = fftinv(modes, norm='ortho')
        modes = jnp.fft.rfftn(modes)

        # TF: padding for antialiasing (factor of (3/2)**3. for the change in dimension)
        modes_NG = jnp.fft.fftshift(modes,axes=[0,1])
        modes_NG = jnp.pad(modes_NG, ((conf.ptcl_grid_shape[0]//4,conf.ptcl_grid_shape[0]//4),(conf.ptcl_grid_shape[1]//4,conf.ptcl_grid_shape[1]//4),(0,conf.ptcl_grid_shape[2]//4))) * (3/2)**3.
        modes_NG = jnp.fft.ifftshift(modes_NG,axes=[0,1])
        
        # TF: square the modes in real space
        modes_NG = jnp.fft.rfftn(jnp.fft.irfftn(modes_NG)**2.) 
        
        # TF: downsampling (factor of (3/2)**3. for the change in dimension)
        modes_NG = jnp.fft.fftshift(modes_NG,axes=[0,1])
        modes_NG = modes_NG[conf.ptcl_grid_shape[0]//4:-conf.ptcl_grid_shape[0]//4, conf.ptcl_grid_shape[1]//4:-conf.ptcl_grid_shape[1]//4,:-conf.ptcl_grid_shape[2]//4] / (3/2)**3.
        modes_NG = jnp.fft.ifftshift(modes_NG,axes=[0,1])
        
        # TF: add to the gaussian modes, factor of 3/5 is because we are generating \zeta and f_nl is defined for \Phi
        modes = jnp.fft.irfftn(modes)
        modes_NG = jnp.fft.irfftn(modes_NG)
        modes = modes + 3/5 * cosmo.f_nl_loc * (modes_NG - jnp.mean(modes_NG))
        modes = modes.astype(conf.float_dtype)

        # TF: apply transfer function
        modes = fftfwd(modes, norm='ortho')
        modes *= Tlin * conf.box_vol / jnp.sqrt(conf.ptcl_num)
    else:
        Plin = linear_power(k, a, cosmo, conf)
        modes *= _safe_sqrt(Plin * conf.box_vol)

    if real:
        modes = fftinv(modes, shape=conf.ptcl_grid_shape, norm=conf.ptcl_spacing)

    return modes