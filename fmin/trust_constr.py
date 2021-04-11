import warnings
import numbers
import torch
import numpy as np
from scipy.optimize import minimize, Bounds, NonlinearConstraint
from scipy.sparse.linalg import LinearOperator

_constr_keys = {'fun', 'lb', 'ub', 'jac', 'hess', 'hessp'}


def _build_funcs(f, x0):

    def to_tensor(x):
        return torch.tensor(x, dtype=x0.dtype, device=x0.device).view_as(x0)

    def f_with_jac(x):
        x = to_tensor(x).requires_grad_(True)
        with torch.enable_grad():
            fval = f(x)
        grad, = torch.autograd.grad(fval, x)
        return fval.detach().cpu().numpy(), grad.view(-1).cpu().numpy()

    def f_hess(x):
        x = to_tensor(x).requires_grad_(True)
        with torch.enable_grad():
            fval = f(x)
            grad, = torch.autograd.grad(fval, x, create_graph=True)
        def matvec(p):
            p = to_tensor(p)
            hvp, = torch.autograd.grad(grad, x, p, retain_graph=True)
            return hvp.view(-1).cpu().numpy()
        return LinearOperator((x.numel(), x.numel()), matvec=matvec)

    return f_with_jac, f_hess


def _build_constr(constr, x0):
    assert isinstance(constr, dict)
    assert set(constr.keys()).issubset(_constr_keys)
    assert 'fun' in constr
    assert 'lb' in constr or 'ub' in constr
    if 'lb' not in constr:
        constr['lb'] = -np.inf
    if 'ub' not in constr:
        constr['ub'] = np.inf
    f_ = constr['fun']
    numel = x0.numel()

    def to_tensor(x):
        return torch.tensor(x, dtype=x0.dtype, device=x0.device).view_as(x0)

    def f(x):
        x = to_tensor(x)
        return f_(x).cpu().numpy()

    def f_jac(x):
        x = to_tensor(x)
        if 'jac' in constr:
            grad = constr['jac'](x)
        else:
            x.requires_grad_(True)
            with torch.enable_grad():
                grad, = torch.autograd.grad(f_(x), x)
        return grad.view(-1).cpu().numpy()

    def f_hess(x, v):
        x = to_tensor(x)
        if 'hess' in constr:
            hess = constr['hess'](x)
            return v[0] * hess.view(numel, numel).cpu().numpy()
        elif 'hessp' in constr:
            def matvec(p):
                p = to_tensor(p)
                hvp = constr['hessp'](x, p)
                return v[0] * hvp.view(-1).cpu().numpy()
            return LinearOperator((numel, numel), matvec=matvec)
        else:
            x.requires_grad_(True)
            with torch.enable_grad():
                if 'jac' in constr:
                    grad = constr['jac'](x)
                else:
                    grad, = torch.autograd.grad(f_(x), x, create_graph=True)
            def matvec(p):
                p = to_tensor(p)
                hvp, = torch.autograd.grad(grad, x, p, retain_graph=True)
                return v[0] * hvp.view(-1).cpu().numpy()
            return LinearOperator((x.numel(), x.numel()), matvec=matvec)

    return NonlinearConstraint(
        fun=f, lb=constr['lb'], ub=constr['ub'],
        jac=f_jac, hess=f_hess)


def _build_bound(val, x0):
    if isinstance(val, numbers.Number):
        return np.full(x0.numel(), val)
    elif isinstance(val, torch.Tensor):
        assert val.numel() == x0.numel()
        return val.detach().cpu().numpy().flatten()
    elif isinstance(val, np.ndarray):
        assert val.size == x0.numel()
        return val
    else:
        raise ValueError('Bound value has unrecognized format.')


@torch.no_grad()
def fmin_trust_constr(
        f, x0, constr=None, bounds=None, max_iter=None, tol=None, callback=None,
        disp=0, **kwargs):
    """
    A constrained minimizer for pytorch functions based on scipy's
    "trust-constr" method.
    """
    if max_iter is None:
        max_iter = 1000
    x0 = x0.detach()
    if x0.is_cuda:
        warnings.warn('GPU is not recommended for trust-constr. '
                      'Data will be moved back-and-forth from CPU.')

    # handle callbacks
    if callback is not None:
        callback_ = callback
        callback = lambda x: callback_(
            torch.tensor(x, dtype=x0.dtype, device=x0.device).view_as(x0))

    # handle bounds
    if bounds is not None:
        assert isinstance(bounds, (tuple, list))
        assert len(bounds) == 2
        lb = _build_bound(bounds[0], x0)
        ub = _build_bound(bounds[1], x0)
        bounds = Bounds(lb, ub)

    # build objective function (and hessian)
    f_with_jac, f_hess = _build_funcs(f, x0)

    # build constraints
    if constr is not None:
        constraints = [_build_constr(constr, x0)]
    else:
        constraints = []

    # optimize
    x0_np = x0.cpu().numpy().flatten().copy()
    result = minimize(
        f_with_jac, x0_np, method='trust-constr', jac=True,
        hess=f_hess, callback=callback, tol=tol,
        bounds=bounds,
        constraints=constraints,
        options=dict(verbose=disp, maxiter=max_iter, **kwargs)
    )

    # convert the important things to torch tensors
    for key in ['fun', 'grad', 'x']:
        result[key] = torch.tensor(result[key], dtype=x0.dtype, device=x0.device)
    result['x'] = result['x'].view_as(x0)

    return result
