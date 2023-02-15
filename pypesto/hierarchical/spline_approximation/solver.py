import warnings
from typing import Dict, List

import numpy as np
from scipy.optimize import minimize

from ...C import MIN_SIM_RANGE, USE_MINIMAL_DIFFERENCE, InnerParameterType
from ..solver import InnerSolver
from .parameter import SplineInnerParameter
from .problem import SplineInnerProblem

try:
    from amici.parameter_mapping import ParameterMapping
except ImportError:
    pass


class SplineInnerSolver(InnerSolver):
    """Solver of the inner subproblem of spline approximation for nonlinear-monotone data.

    Options
    -------
    minimal_difference:
        If True then the method will constrain minimal spline parameter
        difference. Otherwise there will be no such constrain.
    """

    def __init__(self, options: Dict = None):
        self.options = options
        if not self.options:
            self.options = {}

        self.options = {
            **self.get_default_options(),
            **self.options,
        }
        self.validate_options()

    def validate_options(self):
        """Validate the current options dictionary."""
        if self.options[USE_MINIMAL_DIFFERENCE] not in [True, False]:
            raise ValueError('Minimal difference must be a boolean value.')

    def solve(
        self,
        problem: SplineInnerProblem,
        sim: List[np.ndarray],
        sigma: List[np.ndarray],
    ) -> list:
        """Get results for every group (inner optimization problem).

        Parameters
        ----------
        problem:
            InnerProblem from pyPESTO hierarchical.
        sim:
            Simulations from AMICI.
        sigma:
            List of sigmas from AMICI.

        Returns
        -------
        List of optimization results of the inner subproblem.
        """
        inner_optimization_results = []
        for group in problem.get_groups_for_xs(InnerParameterType.SPLINE):
            group_dict = problem.groups[group]
            group_dict['noise_parameters'] = extract_expdata_using_mask(
                expdata=sigma, mask=group_dict['expdata_mask']
            )
            group_dict['current_simulation'] = extract_expdata_using_mask(
                expdata=sim, mask=group_dict['expdata_mask']
            )

            inner_optimization_results_per_group = self._optimize_spline(
                inner_parameters=problem.get_free_xs_for_group(group),
                group_dict=group_dict,
            )
            inner_optimization_results.append(
                inner_optimization_results_per_group
            )
            save_inner_parameters_to_inner_problem(
                inner_parameters=problem.get_xs_for_group(group),
                s=inner_optimization_results_per_group['x'],
            )
        return inner_optimization_results

    @staticmethod
    def calculate_obj_function(x_inner_opt: list):
        """Calculate the inner objective function value.

        Calculates the inner objective function value from a list of inner
        optimization results returned from `_optimize_spline`.

        Parameters
        ----------
        x_inner_opt:
            List of optimization results

        Returns
        -------
        Inner objective function value.
        """
        if False in [
            x_inner_opt[idx]['success'] for idx in range(len(x_inner_opt))
        ]:
            obj = np.inf
            warnings.warn("Inner optimization failed.")
        else:
            obj = np.sum(
                [x_inner_opt[idx]['fun'] for idx in range(len(x_inner_opt))]
            )
        return obj

    def calculate_gradients(
        self,
        problem: SplineInnerProblem,
        x_inner_opt: List[Dict],
        sim: List[np.ndarray],
        sigma: List[np.ndarray],
        sy: List[np.ndarray],
        parameter_mapping: ParameterMapping,
        par_opt_ids: List,
        par_sim_ids: List,
        snllh: Dict,
    ):
        """Calculate gradients of the inner objective function.

        Calculates gradients of the objective function with respect to outer
        (dynamical) parameters.

        Parameters
        ----------
        problem:
            Optimal scaling inner problem.
        x_inner_opt:
            List of optimization results of the inner subproblem.
        sim:
            Model simulations.
        sigma:
            Model noise parameters.
        sy:
            Model sensitivities.
        parameter_mapping:
            Mapping of optimization to simulation parameters.
        par_opt_ids:
            Ids of outer otimization parameters.
        par_sim_ids:
            Ids of outer simulation parameters, includes fixed parameters.
        snllh:
            Empty dictionary with optimization parameters as keys.

        Returns
        -------
        Filled in snllh dictionary with objective function gradients.
        """
        already_calculated = set()

        for condition_map_sim_var in [
            cond_par_map.map_sim_var for cond_par_map in parameter_mapping
        ]:
            for par_sim, par_opt in condition_map_sim_var.items():
                if (
                    not isinstance(par_opt, str)
                    or par_opt in already_calculated
                ):
                    continue
                else:
                    already_calculated.add(par_opt)
                par_sim_idx = par_sim_ids.index(par_sim)
                par_opt_idx = par_opt_ids.index(par_opt)
                grad = 0.0
                sy_for_outer_parameter = [
                    sy_cond[:, par_sim_idx, :] for sy_cond in sy
                ]

                for group_idx, group in enumerate(
                    problem.get_groups_for_xs(InnerParameterType.SPLINE)
                ):
                    # Get the reformulated spline parameters
                    s = np.asarray(x_inner_opt[group_idx]["x"])
                    group_dict = problem.groups[group]

                    measurements = group_dict['datapoints']
                    sigma = group_dict['noise_parameters']
                    sim_all = group_dict['current_simulation']
                    N = group_dict['n_spline_pars']
                    K = group_dict['n_datapoints']

                    sy_all = extract_expdata_using_mask(
                        expdata=sy_for_outer_parameter,
                        mask=group_dict['expdata_mask'],
                    )

                    delta_c, c, n = self._rescale_spline_bases(
                        sim_all=sim_all, N=N, K=K
                    )
                    delta_c_dot, c_dot = calculate_spline_bases_gradient(
                        sim_all=sim_all, sy_all=sy_all, N=N
                    )
                    C = np.diag(-np.ones(N))

                    # For the reformulated problem, mu can be calculated
                    # as the inner gradient at the optimal point s.
                    mu = calculate_inner_gradient(
                        s=s,
                        sim_all=sim_all,
                        measurements=measurements,
                        sigma=sigma,
                        N=N,
                        delta_c=delta_c,
                        c=c,
                        n=n,
                    )
                    min_meas = group_dict['min_datapoint']
                    max_meas = group_dict['max_datapoint']
                    min_diff = self._get_minimal_difference(
                        min_meas=min_meas,
                        max_meas=max_meas,
                        N=N,
                        use_minimal_difference=self.options[
                            USE_MINIMAL_DIFFERENCE
                        ],
                    )

                    # Calculate df_ds term only if mu is not all 0
                    if np.any(mu):
                        s_dot = calculate_ds_dtheta(
                            sim_all=sim_all,
                            sy_all=sy_all,
                            measurements=measurements,
                            sigma=sigma,
                            s=s,
                            C=C,
                            mu=mu,
                            N=N,
                            delta_c=delta_c,
                            delta_c_dot=delta_c_dot,
                            c=c,
                            c_dot=c_dot,
                            n=n,
                            min_diff=min_diff,
                        )
                        df_ds = mu
                        grad += df_ds.dot(s_dot)

                    # Let's calculate the df_dyk term now:
                    df_dyk = calculate_df_dyk(
                        sim_all=sim_all,
                        sy_all=sy_all,
                        measurements=measurements,
                        sigma=sigma,
                        s=s,
                        N=N,
                        delta_c=delta_c,
                        delta_c_dot=delta_c_dot,
                        c=c,
                        c_dot=c_dot,
                        n=n,
                    )

                    grad += df_dyk
                snllh[par_opt_idx] = grad

        return snllh

    @staticmethod
    def get_default_options() -> Dict:
        """Return default options for solving the inner problem."""
        options = {
            "use_minimal_difference": True,
        }
        return options

    def _optimize_spline(
        self,
        inner_parameters: List[SplineInnerParameter],
        group_dict: Dict,
    ):
        """Run optimization for the inner problem.

        Parameters
        ----------
        inner_parameters:
            The spline inner parameters.
        group_dict:
            The group dictionary.
        """
        group_measurements = group_dict['datapoints']
        group_noise_parameters = group_dict['noise_parameters']
        current_group_simulation = group_dict['current_simulation']
        n_datapoints = group_dict['n_datapoints']
        n_spline_pars = group_dict['n_spline_pars']

        (
            distance_between_bases,
            spline_bases,
            intervals_per_sim,
        ) = self._rescale_spline_bases(
            sim_all=current_group_simulation, N=n_spline_pars, K=n_datapoints
        )

        min_meas = group_dict['min_datapoint']
        max_meas = group_dict['max_datapoint']
        min_diff = self._get_minimal_difference(
            min_meas=min_meas,
            max_meas=max_meas,
            N=n_spline_pars,
            use_minimal_difference=self.options[USE_MINIMAL_DIFFERENCE],
        )

        inner_options = self._get_inner_optimization_options(
            inner_parameters=inner_parameters,
            N=n_spline_pars,
            min_meas=min_meas,
            max_meas=max_meas,
            min_diff=min_diff,
        )

        def objective_function_wrapper(x):
            return calculate_objective_function(
                s=x,
                sim_all=current_group_simulation,
                measurements=group_measurements,
                sigma=group_noise_parameters,
                N=n_spline_pars,
                delta_c=distance_between_bases,
                c=spline_bases,
                n=intervals_per_sim,
            )

        def inner_gradient_wrapper(x):
            return calculate_inner_gradient(
                s=x,
                sim_all=current_group_simulation,
                measurements=group_measurements,
                sigma=group_noise_parameters,
                N=n_spline_pars,
                delta_c=distance_between_bases,
                c=spline_bases,
                n=intervals_per_sim,
            )

        results = minimize(
            objective_function_wrapper,
            jac=inner_gradient_wrapper,
            **inner_options,
        )
        # plot_Boehm = True

        # if plot_Boehm:
        #     print("last opt values\n", [x.value for x in inner_parameters])
        #     print("current opt values\n", results['x'])
        #     print("current simulation\n", current_group_simulation)
        #     print("intervals_per_sim\n", intervals_per_sim)
        #     s = results['x']
        #     xi = np.zeros(len(s))
        #     for i in range(len(s)):
        #         for j in range(i, len(s)):
        #             xi[j] += s[i]

        #     mapped_simulations = get_spline_mapped_simulations(
        #         s,
        #         current_group_simulation,
        #         n_spline_pars,
        #         distance_between_bases,
        #         spline_bases,
        #         intervals_per_sim
        #     )
        #     import matplotlib.pyplot as plt
        #     plt.plot(current_group_simulation, group_measurements, 'bs')
        #     plt.plot(spline_bases, xi, 'g')
        #     plt.plot(spline_bases, xi, 'g.')
        #     plt.plot(current_group_simulation, mapped_simulations, 'r^')
        #     plt.show()

        return results

    def _rescale_spline_bases(self, sim_all: np.ndarray, N: int, K: int):
        """Rescale the spline bases.

        Before the optimization of the spline parameters, we have to fix the
        spline bases to some values. We choose to scale them to the current
        simulation. In case of simulations that are very close to each other,
        we choose to scale closely around the average value of the simulations,
        to avoid numerical problems (as we often divide by delta_c).

        Parameters
        ----------
        sim_all:
            The current simulation.
        N:
            The number of spline parameters.
        K:
            The number of simulations.

        Returns
        -------
        distance_between_bases:
            The distance between the spline bases.
        spline_bases:
            The rescaled spline bases.
        intervals_per_sim:
            List of indices of intervals each simulation belongs to.
        """
        min_idx = np.argmin(sim_all)
        max_idx = np.argmax(sim_all)

        min_all = sim_all[min_idx]
        max_all = sim_all[max_idx]

        n = np.ones(K)

        # In case the simulation are very close to each other
        # or even collapse into a single point (e.g. steady-state)
        if max_all - min_all < MIN_SIM_RANGE:
            average_value = (max_all + min_all) / 2
            if average_value < (MIN_SIM_RANGE / 2):
                delta_c = MIN_SIM_RANGE / (N - 1)
                c = np.linspace(0, MIN_SIM_RANGE, N)
            else:
                delta_c = MIN_SIM_RANGE / (N - 1)
                c = np.linspace(
                    average_value - (MIN_SIM_RANGE / 2),
                    average_value + (MIN_SIM_RANGE / 2),
                    N,
                )
            # Set the n(k) values for the simulations
            for i in range(len(sim_all)):
                n[i] = np.ceil((sim_all[i] - c[0]) / delta_c) + 1
                if n[i] > N:
                    n[i] = N
                    warnings.warn(
                        "Interval for a simulation has been set to a larger value than the number of spline parameters."
                    )
        # In case the simulations are sufficiently apart:
        else:
            delta_c = (max_all - min_all) / (N - 1)
            c = np.linspace(min_all, max_all, N)
            for i in range(len(sim_all)):
                if i == max_idx:
                    n[i] = N
                elif i == min_idx:
                    n[i] = 1
                else:
                    n[i] = np.ceil((sim_all[i] - c[0]) / delta_c) + 1
                if n[i] > N:
                    n[i] = N

        n = n.astype(int)
        return delta_c, c, n

    def _get_minimal_difference(
        self,
        min_meas: float,
        max_meas: float,
        N: int,
        use_minimal_difference: bool,
    ):
        """Return minimal parameter difference for spline parameters."""
        if use_minimal_difference:
            min_diff = (max_meas - min_meas) / (2 * N)
        else:
            min_diff = 0
        return min_diff

    def _get_inner_optimization_options(
        self,
        inner_parameters: List[SplineInnerParameter],
        N: int,
        min_meas: float,
        max_meas: float,
        min_diff: float,
    ) -> Dict:
        """Return default options for scipy optimizer.

        Returns inner subproblem optimization options including startpoint
        and optimization bounds or constraints, dependent on solver method.

        Parameters
        ----------
        inner_parameters:
            Inner parameters of the spline group.
        N:
            Number of spline parameters.
        min_meas:
            Minimal measurement value.
        max_meas:
            Maximal measurement value.
        min_diff:
            Minimal difference between spline parameters.
        """
        range_all = max_meas - min_meas

        constraint_min_diff = np.full(N, min_diff)
        constraint_min_diff[0] = 0

        last_opt_values = np.asarray([x.value for x in inner_parameters])

        if (last_opt_values > 0).any():
            x0 = last_opt_values
        else:
            x0 = np.full(
                N,
                (
                    max_meas
                    + 0.3 * range_all
                    - np.max([min_meas - 0.3 * range_all, 0])
                )
                / (N - 1),
            )
            x0[0] = np.max([min_meas - 0.3 * range_all, 0])

        from scipy.optimize import Bounds

        inner_options = {
            "x0": x0,
            "method": "L-BFGS-B",
            "options": {"ftol": 1e-10, "disp": None},
            "bounds": Bounds(lb=constraint_min_diff),
        }

        return inner_options


def calculate_objective_function(
    s: np.ndarray,
    sim_all: np.ndarray,
    measurements: np.ndarray,
    sigma: np.ndarray,
    N: int,
    delta_c: float,
    c: np.ndarray,
    n: np.ndarray,
):
    """Objective function for reformulated inner spline problem."""
    obj = 0

    for y_k, z_k, sigma_k, n_k in zip(sim_all, measurements, sigma, n):
        i = n_k - 1
        sum_s = 0
        sum_s = np.sum(s[:i])
        if i == 0:
            obj += (1 / sigma_k**2) * (z_k - s[i]) ** 2
        elif i == N:
            obj += (1 / sigma_k**2) * (z_k - sum_s) ** 2
        else:
            obj += (1 / sigma_k**2) * (
                z_k - (y_k - c[i - 1]) * s[i] / delta_c - sum_s
            ) ** 2
    obj = obj / 2
    return obj


def get_spline_mapped_simulations(
    s: np.ndarray,
    sim_all: np.ndarray,
    N: int,
    delta_c: float,
    c: np.ndarray,
    n: np.ndarray,
):
    """Return model simulations mapped using the approximation spline."""
    mapped_simulations = np.zeros(len(sim_all))
    xi = np.zeros(len(s))
    for i in range(len(s)):
        xi[i] = np.sum(s[: i + 1])

    for y_k, n_k, index in zip(sim_all, n, range(len(sim_all))):
        interval_index = n_k - 1
        if interval_index == 0 or interval_index == N:
            mapped_simulations[index] = xi[interval_index]
        else:
            mapped_simulations[index] = (y_k - c[interval_index - 1]) * (
                xi[interval_index] - xi[interval_index - 1]
            ) / delta_c + xi[interval_index - 1]

    return mapped_simulations


def calculate_inner_gradient(
    s: np.ndarray,
    sim_all: np.ndarray,
    measurements: np.ndarray,
    sigma: np.ndarray,
    N: int,
    delta_c: float,
    c: np.ndarray,
    n: np.ndarray,
):
    """Gradient of the objective function for the reformulated inner spline problem."""

    gradient = np.zeros(N)

    for y_k, z_k, sigma_k, n_k in zip(sim_all, measurements, sigma, n):
        weight_k = 1 / sigma_k**2
        sum_s = 0
        i = n_k - 1  # just the iterator to go over the Jacobian array
        sum_s = np.sum(s[:i])
        if i == 0:
            gradient[i] += weight_k * (s[i] - z_k)
        elif i == N:
            for j in range(i):
                gradient[j] += weight_k * (sum_s - z_k)
        else:
            gradient[i] += (
                weight_k
                * ((y_k - c[i - 1]) * s[i] / delta_c + sum_s - z_k)
                * (y_k - c[i - 1])
                / delta_c
            )
            gradient[:i] += np.full(
                i,
                weight_k * ((y_k - c[i - 1]) * s[i] / delta_c + sum_s - z_k),
            )
    return gradient


def calculate_inner_hessian(
    s: np.ndarray,
    sim_all: np.ndarray,
    sigma: np.ndarray,
    N: int,
    delta_c: float,
    c: np.ndarray,
    n: np.ndarray,
):
    """Calculate the hessian of the objective function for the reformulated inner problem."""

    hessian = np.zeros((N, N))

    for y_k, sigma_k, n_k in zip(sim_all, sigma, n):
        sum_s = 0
        i = n_k - 1  # just the iterator to go over the Hessian matrix
        for j in range(i):
            sum_s += s[j]

        hessian[i][i] += (1 / sigma_k**2) * ((y_k - c[i - 1]) / delta_c) ** 2
        for j in range(i):
            hessian[i][j] += (1 / sigma_k**2) * ((y_k - c[i - 1]) / delta_c)
            hessian[j][i] += (1 / sigma_k**2) * ((y_k - c[i - 1]) / delta_c)
            for h in range(i):
                hessian[j][h] += 1 / sigma_k**2

    return hessian


def calculate_ds_dtheta(
    sim_all: np.ndarray,
    sy_all: np.ndarray,
    measurements: np.ndarray,
    sigma: np.ndarray,
    s: np.ndarray,
    C: np.ndarray,
    mu: np.ndarray,
    N: int,
    delta_c: float,
    delta_c_dot: float,
    c: np.ndarray,
    c_dot: np.ndarray,
    n: np.ndarray,
    min_diff: float,
):
    """Calculate derivatives of reformulated spline parameters with respect to outer parameter.

    Calculates the derivative of reformulated spline parameters s with respect to the
    dynamical parameter theta. Firstly, we calculate the derivative of the
    first two equations of the necessary optimality conditions of the
    optimization problem with inequality constraints. Then we solve the linear
    system to obtain the derivatives.
    """

    Jacobian_derivative = np.zeros((N, N))
    rhs = np.zeros(2 * N)

    for y_k, z_k, y_dot_k, sigma_k, n_k in zip(
        sim_all, measurements, sy_all, sigma, n
    ):
        i = n_k - 1  # just the iterator to go over the Jacobian matrix
        weight_k = 1 / sigma_k**2
        sum_s = 0
        sum_s = np.sum(s[:i])

        # calculate the Jacobian derivative:
        if i == 0:
            Jacobian_derivative[i][i] += weight_k
        elif i == N:
            Jacobian_derivative = Jacobian_derivative + np.full(
                (N, N), weight_k
            )

        else:
            Jacobian_derivative[i][i] += (
                weight_k * (y_k - c[i - 1]) ** 2 / delta_c**2
            )
            rhs[i] += (
                weight_k
                * (2 * (y_k - c[i - 1]) / delta_c * s[i] + sum_s - z_k)
                * (
                    (y_dot_k - c_dot[i - 1]) * delta_c
                    - (y_k - c[i - 1]) * delta_c_dot
                )
                / delta_c**2
            )
            if i > 0:
                Jacobian_derivative[i, :i] += np.full(
                    i, weight_k * (y_k - c[i - 1]) / delta_c
                )
                Jacobian_derivative[:i, i] += np.full(
                    i, weight_k * (y_k - c[i - 1]) / delta_c
                )
                rhs[:i] += np.full(
                    i,
                    weight_k
                    * (
                        (y_dot_k - c_dot[i - 1]) * delta_c
                        - (y_k - c[i - 1]) * delta_c_dot
                    )
                    * s[i]
                    / delta_c**2,
                )
                Jacobian_derivative[:i, :i] += np.full((i, i), weight_k)

    from scipy import linalg

    constraint_min_diff = np.diag(np.full(N, min_diff))
    constraint_min_diff[0][0] = 0
    lhs = np.block(
        [
            [Jacobian_derivative, C],
            [-np.diag(mu), constraint_min_diff - np.diag(s)],
        ]
    )
    ds_dtheta = linalg.lstsq(lhs, rhs, lapack_driver="gelsy")

    return ds_dtheta[0][:N]


def calculate_df_dyk(
    sim_all: np.ndarray,
    sy_all: np.ndarray,
    measurements: np.ndarray,
    sigma: np.ndarray,
    s: np.ndarray,
    N: int,
    delta_c: float,
    delta_c_dot: float,
    c: np.ndarray,
    c_dot: np.ndarray,
    n: np.ndarray,
):
    """Calculate the derivative of the objective function for one group with respect to the simulations."""
    df_dyk = 0

    for y_k, z_k, y_dot_k, sigma_k, n_k in zip(
        sim_all, measurements, sy_all, sigma, n
    ):
        i = n_k - 1
        sum_s = 0
        sum_s = np.sum(s[:i])
        if i > 0 and i < N:
            df_dyk += (
                (1 / sigma_k**2)
                * ((y_k - c[i - 1]) * s[i] / delta_c + sum_s - z_k)
                * s[i]
                * (
                    (y_dot_k - c_dot[i - 1]) * delta_c
                    - (y_k - c[i - 1]) * delta_c_dot
                )
                / delta_c**2
            )
    return df_dyk


def calculate_spline_bases_gradient(
    sim_all: np.ndarray, sy_all: np.ndarray, N: int
):
    """Calculate gradient of the rescaled spline bases."""

    min_idx = np.argmin(sim_all)
    max_idx = np.argmax(sim_all)

    if sim_all[max_idx] - sim_all[min_idx] < MIN_SIM_RANGE:
        delta_c_dot = 0
        c_dot = np.full(N, (sy_all[max_idx] - sy_all[min_idx]) / 2)
    else:
        delta_c_dot = (sy_all[max_idx] - sy_all[min_idx]) / (N - 1)
        c_dot = np.linspace(sy_all[min_idx], sy_all[max_idx], N)

    return delta_c_dot, c_dot


def extract_expdata_using_mask(
    expdata: List[np.ndarray], mask: List[np.ndarray]
):
    """Extract data from expdata list of arrays for the given mask."""
    return np.concatenate(
        [
            expdata[condition_index][mask[condition_index]]
            for condition_index in range(len(mask))
        ]
    )


def save_inner_parameters_to_inner_problem(
    inner_parameters,
    s,
) -> None:
    """Save inner parameter values to the inner subproblem.

    Calculates the non-reformulated inner spline parameters from
    the reformulated inner spline parameters and saves them to
    the inner subproblem.

    Parameters
    ----------
    inner_parameters : list
        List of inner parameters.
    s : np.ndarray
        Reformulated inner spline parameters.
    """
    xi = np.zeros(len(s))
    for i in range(len(s)):
        xi[i] = np.sum(s[: i + 1])

    for idx in range(len(inner_parameters)):
        inner_parameters[idx].value = xi[idx]


def get_monotonicity_measure(measurement, simulation):
    """Get monotonicity measure by calculating inversions.

    Calculate the number of inversions in the simulation data
    with respect to the measurement data.

    Parameters
    ----------
    measurement : np.ndarray
        Measurement data.
    simulation : np.ndarray
        Simulation data.

    Returns
    -------
    inversions : int
        Number of inversions.
    """
    if len(measurement) != len(simulation):
        raise ValueError(
            "Measurement and simulation data must have the same length."
        )

    ordered_simulation = [
        x
        for _, x in sorted(
            zip(measurement, simulation), key=lambda pair: pair[0]
        )
    ]
    ordered_measurement = sorted(simulation)

    inversions = 0
    for i in range(len(ordered_simulation)):
        for j in range(i + 1, len(ordered_simulation)):
            if ordered_simulation[i] > ordered_simulation[j]:
                inversions += 1
            elif (
                ordered_simulation[i] == ordered_simulation[j]
                and ordered_measurement[i] != ordered_measurement[j]
            ):
                inversions += 1

    return inversions