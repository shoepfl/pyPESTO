import copy
from typing import Dict, List, Sequence, Tuple

import numpy as np

from ...C import FVAL, GRAD, HESS, MODE_RES, RDATAS, RES, SRES, X_INNER_OPT
from ...objective.amici.amici_calculator import AmiciModel, AmiciSolver
from ...objective.amici.amici_util import (
    filter_return_dict,
    init_return_values,
)
from .optimal_scaling_problem import OptimalScalingProblem
from .optimal_scaling_solver import OptimalScalingInnerSolver

try:
    import amici
    from amici.parameter_mapping import ParameterMapping
except ImportError:
    pass


class OptimalScalingAmiciCalculator:
    """A calculator is passed as `calculator` to the pypesto.AmiciObjective.

    While this class cannot be used directly, it has two subclasses
    which allow to use forward or adjoint sensitivity analysis to solve
    a `pypesto.HierarchicalProblem` efficiently in an inner loop, while
    the outer optimization is only concerned with variables not
    specified as `pypesto.HierarchicalParameter`s.
    """

    def __init__(
        self,
        inner_problem: OptimalScalingProblem,
        inner_solver: OptimalScalingInnerSolver = None,
    ):
        """Initialize the calculator from the given problem."""
        self._known_least_squares_safe = False

        self.inner_problem = inner_problem

        if inner_solver is None:
            inner_solver = OptimalScalingInnerSolver()
        self.inner_solver = inner_solver

    def initialize(self):
        """Initialize."""
        self.inner_solver.initialize()

    def __call__(
        self,
        x_dct: Dict,
        sensi_orders: Tuple[int, ...],
        mode: str,
        amici_model: AmiciModel,
        amici_solver: AmiciSolver,
        edatas: List['amici.ExpData'],
        n_threads: int,
        x_ids: Sequence[str],
        parameter_mapping: ParameterMapping,
        fim_for_hess: bool,
    ):
        """Perform the actual AMICI call.

        Parameters
        ----------
        x_dct:
            Parameters for which to compute function value and derivatives.
        sensi_orders:
            Tuple of requested sensitivity orders.
        mode:
            Call mode (function value or residual based).
        amici_model:
            The AMICI model.
        amici_solver:
            The AMICI solver.
        edatas:
            The experimental data.
        n_threads:
            Number of threads for AMICI call.
        x_ids:
            Ids of optimization parameters.
        parameter_mapping:
            Mapping of optimization to simulation parameters.
        fim_for_hess:
            Whether to use the FIM (if available) instead of the Hessian (if
            requested).
        """
        # get dimension of outer problem
        dim = len(x_ids)

        # initialize return values
        nllh, snllh, s2nllh, chi2, res, sres = init_return_values(
            sensi_orders, mode, dim
        )
        # set order in solver
        sensi_order = 0
        if sensi_orders:
            sensi_order = max(sensi_orders)

        amici_solver.setSensitivityOrder(sensi_order)

        x_dct = copy.deepcopy(x_dct)

        # fill in parameters
        amici.parameter_mapping.fill_in_parameters(
            edatas=edatas,
            problem_parameters=x_dct,
            scaled_parameters=True,
            parameter_mapping=parameter_mapping,
            amici_model=amici_model,
        )
        # run amici simulation
        inner_rdatas = amici.runAmiciSimulations(
            amici_model,
            amici_solver,
            edatas,
            num_threads=min(n_threads, len(edatas)),
        )
        inner_result = {
            FVAL: nllh,
            GRAD: snllh,
            HESS: s2nllh,
            RES: res,
            SRES: sres,
            RDATAS: inner_rdatas,
            X_INNER_OPT: self.inner_problem.get_inner_parameter_dictionary(),
        }
        # TODO is this needed?
        if (
            not self._known_least_squares_safe
            and mode == MODE_RES
            and 1 in sensi_orders
        ):
            if not amici_model.getAddSigmaResiduals() and any(
                (
                    (r['ssigmay'] is not None and np.any(r['ssigmay']))
                    or (r['ssigmaz'] is not None and np.any(r['ssigmaz']))
                )
                for r in inner_rdatas
            ):
                raise RuntimeError(
                    'Cannot use least squares solver with'
                    'parameter dependent sigma! Support can be '
                    'enabled via '
                    'amici_model.setAddSigmaResiduals().'
                )
            self._known_least_squares_safe = True  # don't check this again

        # if any amici simulation failed, it's unlikely we can compute
        # meaningful inner parameters, so we better just fail early.
        if any(rdata.status != amici.AMICI_SUCCESS for rdata in inner_rdatas):
            # if the gradient was requested, we need to provide some value
            # for it
            inner_result[FVAL] = np.inf
            if 1 in sensi_orders:
                inner_result[GRAD] = np.full(
                    shape=len(x_ids), fill_value=np.nan
                )
            return filter_return_dict(inner_result)

        sim = [rdata['y'] for rdata in inner_rdatas]
        # clip simulation?

        # compute optimal inner parameters
        x_inner_opt = self.inner_solver.solve(self.inner_problem, sim)
        inner_result[FVAL] = self.inner_solver.calculate_obj_function(
            x_inner_opt
        )
        inner_result[
            X_INNER_OPT
        ] = self.inner_problem.get_inner_parameter_dictionary()

        # calculate analytical gradients if requested
        if sensi_order > 0:
            sy = [rdata['sy'] for rdata in inner_rdatas]
            inner_result[GRAD] = self.inner_solver.calculate_gradients(
                problem=self.inner_problem,
                x_inner_opt=x_inner_opt,
                sim=sim,
                sy=sy,
                parameter_mapping=parameter_mapping,
                par_opt_ids=x_ids,
                snllh=snllh,
            )

        return filter_return_dict(inner_result)
