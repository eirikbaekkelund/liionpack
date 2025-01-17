#
# Solvers
#
import liionpack as lp
from liionpack.solver_utils import _create_casadi_objects as cco
from liionpack.solver_utils import _serial_step as ss
from liionpack.solver_utils import _mapped_step as ms
from liionpack.solver_utils import _serial_eval as se
from liionpack.solver_utils import _mapped_eval as me
import ray
import numpy as np
import time as ticker
from dask.distributed import Client
from tqdm import tqdm
import pybamm


class generic_actor:
    def __init__(self):
        pass

    def setup(
        self,
        Nspm,
        sim_func,
        parameter_values,
        dt,
        inputs,
        variable_names,
        initial_soc,
        nproc,
    ):
        # Casadi specific arguments
        if nproc > 1:
            mapped = True
        else:
            mapped = False
        self.Nspm = Nspm
        # Set up simulation
        self.parameter_values = parameter_values
        if initial_soc is not None:
            if (
                (type(initial_soc) in [float, int])
                or (type(initial_soc) is list and len(initial_soc) == 1)
                or (type(initial_soc) is np.ndarray and len(initial_soc) == 1)
            ):
                _, _ = lp.update_init_conc(parameter_values, initial_soc, update=True)
            else:
                lp.logger.warning(
                    "Using a list or an array of initial_soc "
                    + "is not supported, please set the initial "
                    + "concentrations via inputs"
                )
        if sim_func is None:
            self.simulation = lp.basic_simulation(self.parameter_values)
        else:
            self.simulation = sim_func(parameter_values)

        # Set up integrator
        casadi_objs = cco(
            inputs, self.simulation, dt, Nspm, nproc, variable_names, mapped
        )
        self.model = self.simulation.built_model
        self.integrator = casadi_objs["integrator"]
        self.variables_fn = casadi_objs["variables_fn"]
        self.t_eval = casadi_objs["t_eval"]
        self.event_names = casadi_objs["event_names"]
        self.events_fn = casadi_objs["events_fn"]
        self.step_solutions = casadi_objs["initial_solutions"]
        self.last_events = None
        self.event_change = None
        if mapped:
            self.step_fn = ms
            self.eval_fn = me
        else:
            self.step_fn = ss
            self.eval_fn = se

    def step(self, inputs):
        # Solver Step
        self.step_solutions, self.var_eval, self.events_eval = self.step_fn(
            self.simulation.built_model,
            self.step_solutions,
            inputs,
            self.integrator,
            self.variables_fn,
            self.t_eval,
            self.events_fn,
        )
        return self.check_events()

    def evaluate(self, inputs):
        self.var_eval = self.eval_fn(
            self.simulation.built_model,
            self.step_solutions,
            inputs,
            self.variables_fn,
            self.t_eval,
        )

    def check_events(self):
        if self.last_events is not None:
            # Compare changes
            new_sign = np.sign(self.events_eval)
            old_sign = np.sign(self.last_events)
            self.event_change = (old_sign * new_sign) < 0
            self.last_events = self.events_eval
            return np.any(self.event_change)
        else:
            self.last_events = self.events_eval
            return False

    def get_event_change(self):
        return self.event_change

    def get_event_names(self):
        return self.event_names

    def output(self):
        return self.var_eval


@ray.remote(num_cpus=1)
class ray_actor(generic_actor):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)


class generic_manager:
    def __init__(
        self,
    ):
        pass

    def solve(
        self,
        netlist,
        sim_func,
        parameter_values,
        experiment,
        inputs,
        output_variables,
        initial_soc,
        nproc,
    ):
        self.netlist = netlist
        self.sim_func = sim_func

        self.parameter_values = parameter_values
        self.check_current_function()
        # Get netlist indices for resistors, voltage sources, current sources
        Ri_map = netlist["desc"].str.find("Ri") > -1
        V_map = netlist["desc"].str.find("V") > -1
        I_map = netlist["desc"].str.find("I") > -1
        Terminal_Node = np.array(netlist[I_map].node1)
        Nspm = np.sum(V_map)

        self.split_models(Nspm, nproc)

        # Generate the protocol from the supplied experiment
        protocol = lp.generate_protocol_from_experiment(experiment, flatten=True)
        self.dt = experiment.period
        Nsteps = len(protocol)
        netlist.loc[I_map, ("value")] = protocol[0]
        # Solve the circuit to initialise the electrochemical models
        V_node, I_batt = lp.solve_circuit_vectorized(netlist)

        # The simulation output variables calculated at each step for each battery
        # Must be a 0D variable i.e. battery wide volume average - or X-averaged for
        # 1D model
        self.variable_names = [
            "Terminal voltage [V]",
            "Measured battery open circuit voltage [V]",
        ]
        if output_variables is not None:
            for out in output_variables:
                if out not in self.variable_names:
                    self.variable_names.append(out)
            # variable_names = variable_names + output_variables
        Nvar = len(self.variable_names)

        # Storage variables for simulation data
        self.shm_i_app = np.zeros([Nsteps, Nspm], dtype=float)
        self.shm_Ri = np.zeros([Nsteps, Nspm], dtype=float)
        self.output = np.zeros([Nvar, Nsteps, Nspm], dtype=float)

        # Initialize currents in battery models
        self.shm_i_app[0, :] = I_batt * -1

        # Step forward in time
        self.timestep = 0
        V_terminal = []
        record_times = []

        v_cut_lower = parameter_values["Lower voltage cut-off [V]"]
        v_cut_higher = parameter_values["Upper voltage cut-off [V]"]

        # Handle the inputs
        self.inputs = inputs
        self.inputs_dict = lp.build_inputs_dict(self.shm_i_app[0, :], self.inputs)
        # Solver specific setup
        self.setup_actors(nproc, self.inputs_dict, initial_soc)
        # Get the initial state of the system
        self.evaluate_actors()
        sim_start_time = ticker.time()
        lp.logger.notice("Starting step solve")
        with tqdm(total=Nsteps, desc="Stepping simulation") as pbar:
            step = 0
            while step < Nsteps:
                # 01 Calculate whether resting or restarting
                self.resting = (
                    step > 0 and protocol[step] == 0.0 and protocol[step - 1] == 0.0
                )
                self.restarting = (
                    step > 0 and protocol[step] != 0.0 and protocol[step - 1] == 0.0
                )
                # 02 Get the actor output - Battery state info
                self.get_actor_output(step)
                # 03 Get the ocv and internal resistance
                temp_v = self.output[0, step, :]
                temp_ocv = self.output[1, step, :]
                # When resting and rebalancing currents are small the internal
                # resistance calculation can diverge as it's R = V / I
                # At rest the internal resistance should not change greatly
                # so for now just don't recalculate it.
                if not self.resting and not self.restarting:
                    temp_Ri = self.calculate_internal_resistance(step)
                self.shm_Ri[step, :] = temp_Ri
                # 04 Update netlist
                netlist.loc[V_map, ("value")] = temp_ocv
                netlist.loc[Ri_map, ("value")] = temp_Ri
                netlist.loc[I_map, ("value")] = protocol[step]
                # 05 Solve the circuit with updated netlist
                if step <= Nsteps:
                    V_node, I_batt = lp.solve_circuit_vectorized(netlist)
                    record_times.append((step) * self.dt)
                    V_terminal.append(V_node[Terminal_Node][0])
                if step < Nsteps - 1:
                    # igore last step save the new currents and build inputs
                    # for the next step
                    I_app = I_batt[:] * -1
                    self.shm_i_app[step + 1, :] = I_app
                    self.inputs_dict = lp.build_inputs_dict(I_app, self.inputs)
                # 06 Check if voltage limits are reached and terminate
                if np.any(temp_v < v_cut_lower):
                    lp.logger.warning("Low voltage limit reached")
                    break
                if np.any(temp_v > v_cut_higher):
                    lp.logger.warning("High voltage limit reached")
                    break
                # 07 Step the electrochemical system
                self.step_actors()
                # 08 increment the step and update progress bar
                step += 1
                self.timestep = step
                pbar.update(1)

        lp.logger.notice("Step solve finished")
        self.cleanup()
        self.shm_Ri = np.abs(self.shm_Ri)
        # Collect outputs
        self.all_output = {}
        self.all_output["Time [s]"] = np.asarray(record_times)
        self.all_output["Pack current [A]"] = np.asarray(protocol[: step + 1])
        self.all_output["Pack terminal voltage [V]"] = np.asarray(V_terminal)
        self.all_output["Cell current [A]"] = self.shm_i_app[: step + 1, :]
        self.all_output["Cell internal resistance [Ohm]"] = self.shm_Ri[: step + 1, :]
        for j in range(Nvar):
            self.all_output[self.variable_names[j]] = self.output[j, : step + 1, :]

        toc = ticker.time()

        lp.logger.notice(
            "Total stepping time " + str(np.around(toc - sim_start_time, 3)) + "s"
        )
        lp.logger.notice(
            "Time per step " + str(np.around((toc - sim_start_time) / Nsteps, 3)) + "s"
        )
        return self.all_output

    def check_current_function(self):
        i_func = self.parameter_values["Current function [A]"]
        if i_func.__class__ is not pybamm.InputParameter:
            self.parameter_values.update({"Current function [A]": "[input]"})
            lp.logger.notice(
                "Parameter: Current function [A] has been set to " + "input"
            )

    def actor_i_app(self, index):
        actor_indices = self.split_index[index]
        return self.shm_i_app[self.timestep, actor_indices]

    def actor_htc(self, index):
        return self.htc[index]

    def build_inputs(self):
        inputs = []
        for i in range(len(self.actors)):
            inputs.append(self.inputs_dict[self.slices[i]])
        return inputs

    def calculate_internal_resistance(self, step):
        # Calculate internal resistance and update netlist
        temp_v = self.output[0, step, :]
        temp_ocv = self.output[1, step, :]
        temp_I = self.shm_i_app[step, :]
        temp_Ri = np.abs((temp_ocv - temp_v) / temp_I)
        return temp_Ri

    def split_models(self, Nspm, nproc):
        pass

    def setup_actors(self, nproc, inputs, initial_soc):
        pass

    def step_actors(self):
        pass

    def evaluate_actors(self):
        pass

    def get_actor_output(self, step):
        pass

    def cleanup(self):
        pass


class ray_manager(generic_manager):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        lp.logger.notice("Ray initialization started")
        ray.init()
        lp.logger.notice("Ray initialization complete")

    def split_models(self, Nspm, nproc):
        # Manage the number of SPM models per worker
        self.split_index = np.array_split(np.arange(Nspm), nproc)
        self.spm_per_worker = [len(s) for s in self.split_index]
        self.slices = []
        for i in range(nproc):
            self.slices.append(
                slice(self.split_index[i][0], self.split_index[i][-1] + 1)
            )

    def setup_actors(self, nproc, inputs, initial_soc):
        tic = ticker.time()
        # Ray setup an actor for each worker
        self.actors = []
        for i in range(nproc):
            self.actors.append(lp.ray_actor.remote())
        setup_futures = []
        for i, a in enumerate(self.actors):
            # Create actor on each worker containing a simulation
            setup_futures.append(
                a.setup.remote(
                    Nspm=self.spm_per_worker[i],
                    sim_func=self.sim_func,
                    parameter_values=self.parameter_values,
                    dt=self.dt,
                    inputs=inputs[self.slices[i]],
                    variable_names=self.variable_names,
                    initial_soc=initial_soc,
                    nproc=1,
                )
            )
        _ = [ray.get(f) for f in setup_futures]
        toc = ticker.time()
        lp.logger.notice(
            "Ray actors setup in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def step_actors(self):
        t1 = ticker.time()
        future_steps = []
        inputs = self.build_inputs()
        for i, pa in enumerate(self.actors):
            future_steps.append(pa.step.remote(inputs[i]))
        events = [ray.get(fs) for fs in future_steps]
        if np.any(events):
            self.log_event()
        t2 = ticker.time()
        lp.logger.info("Ray actors stepped in " + str(np.around(t2 - t1, 3)) + "s")

    def evaluate_actors(self):
        t1 = ticker.time()
        future_evals = []
        inputs = self.build_inputs()
        for i, pa in enumerate(self.actors):
            future_evals.append(pa.evaluate.remote(inputs[i]))
        _ = [ray.get(fs) for fs in future_evals]
        t2 = ticker.time()
        lp.logger.info("Ray actors evaluated in " + str(np.around(t2 - t1, 3)) + "s")

    def get_actor_output(self, step):
        t1 = ticker.time()
        futures = []
        for actor in self.actors:
            futures.append(actor.output.remote())
        for i, f in enumerate(futures):
            out = ray.get(f)
            self.output[:, step, self.split_index[i]] = out
        t2 = ticker.time()
        lp.logger.info(
            "Ray actor output retrieved in " + str(np.around(t2 - t1, 3)) + "s"
        )

    def log_event(self):
        futures = []
        for actor in self.actors:
            futures.append(actor.get_event_change.remote())
        all_event_changes = []
        for i, f in enumerate(futures):
            all_event_changes.append(np.asarray(ray.get(f)))
        event_change = np.hstack(all_event_changes)
        Nr, Nc = event_change.shape
        event_names = ray.get(self.actors[0].get_event_names.remote())
        for r in range(Nr):
            if np.any(event_change[r, :]):
                lp.logger.warning(
                    event_names[r]
                    + ", Batteries: "
                    + str(np.where(event_change[r, :])[0].tolist())
                )

    def cleanup(self):
        for actor in self.actors:
            ray.kill(actor)
        lp.logger.notice("Shutting down Ray")
        ray.shutdown()


class casadi_manager(generic_manager):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def split_models(self, Nspm, nproc):
        # For casadi there is no need to split the models as we pass them all
        # to the integrator however we still want the global variables to be
        # used in the same generic way
        self.spm_per_worker = Nspm
        self.split_index = np.array_split(np.arange(Nspm), 1)
        self.slices = [slice(self.split_index[0][0], self.split_index[0][-1] + 1)]

    def setup_actors(self, nproc, inputs, initial_soc):
        # For casadi we do not use multiple actors but instead the integrator
        # function that is generated by casadi handles multithreading behind
        # the scenes
        tic = ticker.time()

        self.actors = [generic_actor()]
        for a in self.actors:
            a.setup(
                Nspm=self.spm_per_worker,
                sim_func=self.sim_func,
                parameter_values=self.parameter_values,
                dt=self.dt,
                inputs=inputs,
                variable_names=self.variable_names,
                initial_soc=initial_soc,
                nproc=nproc,
            )
        toc = ticker.time()
        lp.logger.info(
            "Casadi actor setup in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def step_actors(self):
        tic = ticker.time()
        events = self.actors[0].step(self.build_inputs()[0])
        if events:
            self.log_event()
        toc = ticker.time()
        lp.logger.info(
            "Casadi actor stepped in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def evaluate_actors(self):
        tic = ticker.time()
        self.actors[0].evaluate(self.build_inputs()[0])
        toc = ticker.time()
        lp.logger.info(
            "Casadi actor evaluated in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def get_actor_output(self, step):
        tic = ticker.time()
        self.output[:, step, :] = self.actors[0].output()
        toc = ticker.time()
        lp.logger.info(
            "Casadi actor output got in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def log_event(self):
        event_change = np.asarray(self.actors[0].get_event_change())
        Nr, Nc = event_change.shape
        event_names = self.actors[0].get_event_names()
        for r in range(Nr):
            if np.any(event_change[r, :]):
                lp.logger.warning(
                    event_names[r]
                    + ", Batteries: "
                    + str(np.where(event_change[r, :])[0].tolist())
                )

    def cleanup(self):
        pass


class dask_manager(generic_manager):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def split_models(self, Nspm, nproc):
        # Manage the number of SPM models per worker
        self.split_index = np.array_split(np.arange(Nspm), nproc)
        self.spm_per_worker = [len(s) for s in self.split_index]
        self.slices = []
        for i in range(nproc):
            self.slices.append(
                slice(self.split_index[i][0], self.split_index[i][-1] + 1)
            )

    def setup_actors(self, nproc, inputs, initial_soc):
        # Set up a casadi actor on each process
        lp.logger.notice("Dask initialization started")
        self.client = Client(n_workers=nproc)
        lp.logger.notice("Dask initialization complete")
        tic = ticker.time()
        futures = []
        for i in range(nproc):
            # Create actor on each worker containing a simulation
            futures.append(self.client.submit(generic_actor, actor=True, pure=False))
        self.actors = [af.result() for af in futures]
        futures = []
        for i, a in enumerate(self.actors):
            futures.append(
                a.setup(
                    Nspm=self.spm_per_worker[i],
                    sim_func=self.sim_func,
                    parameter_values=self.parameter_values,
                    inputs=inputs[self.slices[i]],
                    dt=self.dt,
                    variable_names=self.variable_names,
                    initial_soc=initial_soc,
                    nproc=1,
                )
            )

        _ = [af.result() for af in futures]
        toc = ticker.time()
        lp.logger.info(
            "Dask actors setup in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def step_actors(self):
        tic = ticker.time()
        inputs = self.build_inputs()
        future_steps = []
        for i, a in enumerate(self.actors):
            future_steps.append(a.step(inputs=inputs[i]))
        events = [af.result() for af in future_steps]
        if np.any(events):
            self.log_event()
        toc = ticker.time()
        lp.logger.info(
            "Dask actors stepped in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def evaluate_actors(self):
        tic = ticker.time()
        inputs = self.build_inputs()
        future_evals = []
        for i, a in enumerate(self.actors):
            future_evals.append(a.evaluate(inputs=inputs[i]))
        _ = [af.result() for af in future_evals]
        toc = ticker.time()
        lp.logger.info(
            "Dask actors evaluated in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def get_actor_output(self, step):
        tic = ticker.time()
        future_gets = []
        for i, a in enumerate(self.actors):
            future_gets.append(a.output())
        for i, fg in enumerate(future_gets):
            out = fg.result()
            self.output[:, step, self.split_index[i]] = out
        toc = ticker.time()
        lp.logger.info(
            "Dask,actors output got in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def log_event(self):
        futures = []
        for actor in self.actors:
            futures.append(actor.get_event_change())
        all_event_changes = []
        for i, f in enumerate(futures):
            all_event_changes.append(np.asarray(f.result()))
        event_change = np.hstack(all_event_changes)
        Nr, Nc = event_change.shape
        event_names = self.actors[0].get_event_names().result()
        for r in range(Nr):
            if np.any(event_change[r, :]):
                lp.logger.warning(
                    event_names[r]
                    + ", Batteries: "
                    + str(np.where(event_change[r, :])[0].tolist())
                )

    def cleanup(self):
        lp.logger.notice("Shutting down Dask client")
        self.client.shutdown()
