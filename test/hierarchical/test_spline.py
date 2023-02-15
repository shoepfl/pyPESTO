from pathlib import Path
from typing import Dict, List

import numpy as np
import petab
import pytest

import pypesto
import pypesto.logging
import pypesto.optimize
import pypesto.petab
from pypesto.C import LIN, MODE_FUN, InnerParameterType
from pypesto.hierarchical.spline_approximation import (
    SplineInnerProblem,
    SplineInnerSolver,
)
from pypesto.hierarchical.spline_approximation.parameter import (
    SplineInnerParameter,
)
from pypesto.hierarchical.spline_approximation.solver import (
    extract_expdata_using_mask,
    get_monotonicity_measure,
    get_spline_mapped_simulations,
)

inner_solver_options = [
    [
        {
            'spline_ratio': spline_ratio,
            'use_minimal_difference': use_minimal_difference,
        }
        for spline_ratio in [1, 1 / 2, 1 / 3, 1 / 4]
    ]
    for use_minimal_difference in [True, False]
]

example_nonlinear_monotone_yaml = (
    Path(__file__).parent
    / '..'
    / '..'
    / 'doc'
    / 'example'
    / 'example_nonlinear_monotone'
    / 'example_nonlinear_monotone.yaml'
)


@pytest.fixture(params=inner_solver_options)
def inner_solver_options(request):
    return request.param


def test_optimization(inner_solver_options: List[Dict]):
    """Check that optimizations finishes without error."""
    petab_problem = petab.Problem.from_yaml(example_nonlinear_monotone_yaml)

    optimizer = pypesto.optimize.ScipyOptimizer(
        method="L-BFGS-B",
        options={"disp": None, "ftol": 2.220446049250313e-09, "gtol": 1e-5},
    )
    for option in inner_solver_options:
        problem = _create_problem(petab_problem, option)
        pypesto.optimize.minimize(
            problem=problem, n_starts=1, optimizer=optimizer
        )


def _create_problem(
    petab_problem: petab.Problem, option: Dict
) -> pypesto.Problem:
    """Creates the spline pyPESTO problem with given options."""
    importer = pypesto.petab.PetabImporter(
        petab_problem, nonlinear_monotone=True
    )
    importer.create_model()

    objective = importer.create_objective(
        inner_solver_options=option,
    )
    problem = importer.create_problem(objective)
    return problem


def test_optimal_scaling_calculator_and_objective():
    """Test the spline calculation of objective values."""
    petab_problem = petab.Problem.from_yaml(example_nonlinear_monotone_yaml)

    problems = {}
    options = {
        'minimal_diff_on': {
            'spline_ratio': 1 / 2,
            'use_minimal_difference': True,
        },
        'minimal_diff_off': {
            'spline_ratio': 1 / 2,
            'use_minimal_difference': False,
        },
    }

    for minimal_diff, option in options.items():
        importer = pypesto.petab.PetabImporter(
            petab_problem, nonlinear_monotone=True
        )
        objective = importer.create_objective(
            inner_solver_options=option,
        )
        problem = importer.create_problem(objective)
        problems[minimal_diff] = problem

    def calculate(problem, x_dct):
        return problem.objective.calculator(
            x_dct=x_dct,
            sensi_orders=(0, 1),
            mode=MODE_FUN,
            amici_model=problem.objective.amici_model,
            amici_solver=problem.objective.amici_solver,
            edatas=problem.objective.edatas,
            n_threads=1,
            x_ids=petab_problem.x_ids,
            parameter_mapping=problem.objective.parameter_mapping,
            fim_for_hess=False,
        )

    x_dct = dict(zip(petab_problem.x_ids, petab_problem.x_nominal_scaled))

    calculator_results = {
        minimal_diff: calculate(problems[minimal_diff], x_dct=x_dct)
        for minimal_diff in options.keys()
    }

    # For nominal parameters, the objective function and gradient
    # will not depend on whether we constrain minimal difference.
    # In general, this is not the case.
    assert np.isclose(
        calculator_results['minimal_diff_on']['fval'],
        calculator_results['minimal_diff_off']['fval'],
    )
    assert np.isclose(
        calculator_results['minimal_diff_on']['grad'],
        calculator_results['minimal_diff_off']['grad'],
    ).all()

    # Since the nominal parameters are close to true ones, the
    # the fval and grad should both be low.
    assert np.all(calculator_results['minimal_diff_on']['fval'] < 1e-4)
    assert np.all(calculator_results['minimal_diff_off']['grad'] < 1e-4)


def test_extract_expdata_using_mask():
    """Test the extraction of expdata using a mask."""
    expdata = [
        np.array([1, 2, 3, 4, 5]),
        np.array([6, 7, 8, 9, 10]),
    ]
    mask = [
        np.array([True, False, True, False, True]),
        np.array([False, True, False, True, False]),
    ]
    assert np.all(
        extract_expdata_using_mask(expdata, mask) == np.array([1, 3, 5, 7, 9])
    )


def test_get_monotonicity_measure():
    """Test the calculation of the monotonicity measure."""
    measurement = np.array([1, 2, 3, 4, 5])
    simulation = np.array([1, 2, 3, 4, 5])
    assert get_monotonicity_measure(measurement, simulation) == 0

    measurement = np.array([1, 2, 3, 4, 5])
    simulation = np.array([5, 4, 3, 2, 1])
    assert get_monotonicity_measure(measurement, simulation) == 10


def _inner_problem_exp():
    timepoints = np.linspace(0, 10, 11)

    simulation = timepoints
    sigma = np.full(len(timepoints), 1)
    data = timepoints

    spline_ratio = 1 / 2
    n_spline_pars = int(np.ceil(spline_ratio * len(timepoints)))

    expected_values = {
        'fun': 0.0,
        'jac': np.zeros(n_spline_pars),
        'x': np.asarray([0.0, 2.0, 2.0, 2.0, 2.0, 2.0]),
    }

    par_type = 'spline'
    mask = [np.full(len(simulation), True)]

    inner_parameters = [
        SplineInnerParameter(
            inner_parameter_id=f'{par_type}_{1}_{par_index+1}',
            inner_parameter_type=InnerParameterType.SPLINE,
            scale=LIN,
            lb=-np.inf,
            ub=np.inf,
            ixs=mask,
            index=par_index + 1,
            group=1,
        )
        for par_index in range(n_spline_pars)
    ]

    inner_problem = SplineInnerProblem(
        xs=inner_parameters, data=[data], spline_ratio=spline_ratio
    )

    return inner_problem, expected_values, simulation, sigma


def test_spline_inner_solver():
    """Test the spline inner solver."""
    inner_problem, expected_values, simulation, sigma = _inner_problem_exp()

    options = {
        'minimal_diff_on': {
            'spline_ratio': 1 / 2,
            'use_minimal_difference': True,
        },
        'minimal_diff_off': {
            'spline_ratio': 1 / 2,
            'use_minimal_difference': False,
        },
    }

    rtol = 1e-6

    inner_solvers = {}
    results = {}

    for minimal_diff, option in options.items():
        inner_solvers[minimal_diff] = SplineInnerSolver(
            options=option,
        )

        results[minimal_diff] = inner_solvers[minimal_diff].solve(
            problem=inner_problem,
            sim=[simulation],
            sigma=[sigma],
        )

    for minimal_diff in options.keys():
        assert np.isclose(
            results[minimal_diff][0]['fun'], expected_values['fun'], rtol=rtol
        )
        assert np.isclose(
            results[minimal_diff][0]['jac'], expected_values['jac'], rtol=rtol
        ).all()
        assert np.isclose(
            results[minimal_diff][0]['x'], expected_values['x'], rtol=rtol
        ).all()


def test_get_spline_mapped_simulations():
    """Test the mapping of model simulations using the spline."""
    spline_parameters = np.array([2, 4, 6, 8, 15])
    simulation = np.array([1, 1.5, 2, 2.5, 3, 3.5, 4, 4.2, 5])
    n_spline_pars = 5
    delta_c = 1
    c = np.array([1, 2, 3, 4, 5])
    simulation_intervals = np.array([1, 2, 3, 3, 4, 4, 5, 5, 5])

    rtol = 1e-6

    expected_spline_mapped_simulations = np.array(
        [2, 4, 6, 9, 12, 16, 20, 23, 35]
    )

    spline_mapped_simulations = get_spline_mapped_simulations(
        spline_parameters,
        simulation,
        n_spline_pars,
        delta_c,
        c,
        simulation_intervals,
    )
    assert np.isclose(
        spline_mapped_simulations,
        expected_spline_mapped_simulations,
        rtol=rtol,
    ).all()