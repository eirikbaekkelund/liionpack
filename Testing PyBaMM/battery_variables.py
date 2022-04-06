import pybamm
import numpy as np
from sympy import evaluate
import scipy.optimize
import numbers

def check_input(name, params, t_init, t_final, intervals):
    """
    Check if inputs is of the correct type

    """
    if isinstance(name, str) == False:
        raise ValueError("name must be of type string")
    if isinstance(params, dict) == False:
        raise ValueError("params must be passed as dict")
    if isinstance(t_init, numbers.Number) == False:
        raise ValueError("time variable must be numeric")
    if isinstance(t_final, numbers.Number) == False:
        raise ValueError("time variable must be numeric")
    if isinstance(intervals, int) == False:
        raise ValueError("intervals must be an integer")




def add_variable(model, name, params, t_init, t_final, intervals):
    """
    Creates a PyBaMM parameter with corresponding degredation function to measure e.g. State of Charge & State of Health

    Parameters:
    model: PyBaMM model object
    name: string name of the parameter
    params: dict of neccessary parameters for function modeling. Keys must be strings, Values must be numeric initial values

    Returns:
    ???
    """
    
    #check_input(name, params, t_init, t_final, intervals)

    var = pybamm.Variable(name)
    func_params = {}
    variables = []

    for key in params.keys():
        func_params[key] = pybamm.Parameter(key)
        variables.append(func_params[key])
    
    #The line below must be user specified, no code exist for versitile function implementation
    # Could we implement an equation here for say SOH / SOC using the equations Rodrigo had from research?
    model.rhs = {var: variables[1] ** variables[2]}
    model.initial_conditions = {var : params[name]}
    
    param_vals = {}

    for key in func_params.keys():
        param_vals[key] = pybamm.ParameterValues({key : params[key]})
        param_vals[key].process_model(model)
    
    print(model.rhs[var])

    solver = pybamm.CasadiSolver( mode = 'fast', rtol = 1e-10, atol = 1e-10)
    time = np.linspace(t_init, t_final, intervals)
    sol = solver.solve(model, time)

    return sol
    
        


