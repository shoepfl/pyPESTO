"""
Spline approach
============

Contains the implementation of the spline approach, applied for integration
of nonlinear-monotone data in ODE modeling, where the data is assumed to have
an unknown monotone relationship with the model output. This relationship is
approximated by a piecewise-linear (spline) function, which is numerically
optimized to fit the data. This constitutes the inner subproblem of the
hierarchical optimization problem.
"""

from .calculator import SplineAmiciCalculator
from .problem import SplineInnerProblem
from .solver import SplineInnerSolver
