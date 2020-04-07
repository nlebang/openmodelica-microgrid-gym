from datetime import datetime
import re
import logging
from os.path import basename
from typing import Sequence, Callable, List, Union, Tuple

import gym
import numpy as np
import pandas as pd
import scipy
from pyfmi import load_fmu
from pyfmi.fmi import FMUModelME2
from scipy import integrate
from fnmatch import translate
import matplotlib.pyplot as plt

from gym_microgrid.common.itertools_ import flatten
from gym_microgrid.env.recorder import FullHistory, EmptyHistory

logger = logging.getLogger(__name__)


class ModelicaEnv(gym.Env):
    """
    OpenAI gym Environment encapsulating an FMU model.
    """

    viz_modes = {'episode', 'step', None}

    def __init__(self, time_step: float = 1e-4, time_start=0, reward_fun: callable = lambda obs: 1,
                 log_level: int = logging.WARNING, solver_method='LSODA', max_episode_steps: int = None,
                 model_params: dict = None,
                 model_input: Sequence[str] = None, model_output: Sequence[str] = None, model_path='grid.network.fmu',
                 viz_mode: str = 'episode', selected_viz_series=None, history: EmptyHistory = FullHistory()):
        """
        Initialize the Environment.
        The environment can only be used after reset() is called.

        :type time_step: float
        :param time_step: step size of the simulation in seconds

        :type time_step: float
        :param time_start: offset of the time in seconds

        :type reward_fun: callable
        :param reward_fun:
            The function receives the observation as a DataFrame and must return the reward of this timestep as float.

        :type log_level: int
        :param log_level: logging granularity. see logging in stdlib

        :type solver_method: str
        :param solver_method: solver of the scipy.integrate.solve_ivp function

        :type max_episode_steps: int
        :param max_episode_steps: maximum number of episode steps.
            The end time of the episode is calculated by the time resolution and the number of steps.

        :type model_params: list of strings
        :param model_params: parameters of the FMU.

            dictionary of variable names and scalars or callables.
            If a callable is provided it is called every time step with the current time.

        :param model_input:
        :param model_output:
        :param model_path:

        :type viz_mode: string, optional
        :param viz_mode: specifies how and if to render

            - 'episode': render after the episode is finished
            - 'step': render after each time step
            - None: disable visualization

        :type selected_viz_series: list, str, optional
        :param selected_viz_series: enables specific columns while plotting
             - None: all columns will be used for vizualization (default)
             - string: will be interpret as regex. all fully matched columns names will be enabled
             - list of strings: Each string might be a unix-shell style wildcard like "*.i"
                                to match all data series ending with ".i".

        :type history: EmptyHistory
        :param history: history to store observations and measurements (from the agent) after each step
        """
        logger.setLevel(log_level)
        # Right now still there until we defined an other stop-criteria according to safeness
        if model_input is None:
            raise ValueError('Please specify model_input variables from your OM FMU.')
        if model_output is None:
            raise ValueError('Please specify model_output variables from your OM FMU.')
        if viz_mode not in self.viz_modes:
            raise ValueError(f'Please select one of the following viz_modes: {self.viz_modes}')

        self.viz_mode = viz_mode
        logger.setLevel(log_level)
        self.solver_method = solver_method

        # load model from fmu
        model_name = basename(model_path)
        logger.debug("Loading model {}".format(model_name))
        self.model: FMUModelME2 = load_fmu(model_path,
                                           log_file_name=datetime.now().strftime(f'%Y-%m-%d_{model_name}.txt'))
        logger.debug("Successfully loaded model {}".format(model_name))

        # if you reward policy is different from just reward/penalty - implement custom step method
        self.reward = reward_fun

        # Parameters required by this implementation
        self.time_start = time_start
        self.time_step_size = time_step
        self.time_end = np.inf if max_episode_steps is None else self.time_start + max_episode_steps * self.time_step_size

        # if there are parameters, we will convert all scalars to constant functions.
        self.model_parameters = model_params and {var: (val if isinstance(val, Callable) else lambda t: val) for
                                                  var, val in model_params.items()}

        self.sim_time_interval = None
        self.__state = None
        self.__measurements = None
        self.record_states = viz_mode == 'episode'
        self.history = history
        self.history.cols = model_output
        self.model_input_names = model_input
        # variable names are flattened to a list if they have specified in the nested dict manner
        self.model_output_names = self.history.cols
        if selected_viz_series is None:
            logging.info('Provide the option "selected_viz_series" if you wish to select only specific plots. '
                         'The default behaviour is to plot all data series')
            self.viz_col_regex = '.*'
        elif isinstance(selected_viz_series, list):
            self.viz_col_regex = '|'.join([translate(glob) for glob in selected_viz_series])
        elif isinstance(selected_viz_series, str):
            # is directly interpret as regex
            self.viz_col_regex = selected_viz_series
        else:
            raise ValueError('"selected_vis_series" must be one of the following:'
                             ' None, str(regex), list of strings (list of shell like globbing patterns) '
                             f'and not {type(selected_viz_series)}')

        # OpenAI Gym requirements
        self.action_space = gym.spaces.Discrete(3)
        high = np.array([self.time_end, np.inf])
        self.observation_space = gym.spaces.Box(-high, high)

    def _setup_fmu(self):
        """
        initialize fmu model in self.model
        :return: None
        """

        self.model.setup_experiment(start_time=self.time_start)
        self.model.enter_initialization_mode()
        self.model.exit_initialization_mode()

        e_info = self.model.get_event_info()
        e_info.newDiscreteStatesNeeded = True
        # Event iteration
        while e_info.newDiscreteStatesNeeded:
            self.model.enter_event_mode()
            self.model.event_update()
            e_info = self.model.get_event_info()

        self.model.enter_continuous_time_mode()

        # precalculating indices for more efficient lookup
        self.model_output_idx = np.array(
            [self.model.get_variable_valueref(k) for k in self.model_output_names])

    def _calc_jac(self, t, x):
        """

        :param t:
        :param x:
        :return:
        """
        # get state and derivative value reference lists
        refs = [[s.value_reference for s in getattr(self.model, attr)().values()]
                for attr in
                ['get_states_list', 'get_derivatives_list']]
        jacobian = np.identity(len(refs[1]))
        np.apply_along_axis(lambda col: self.model.get_directional_derivative(*refs, col), 0, jacobian)
        return jacobian

    def _get_deriv(self, t, x):
        self.model.time = t
        self.model.continuous_states = x.copy(order='C')

        # Compute the derivative
        dx = self.model.get_derivatives()
        return dx

    def _simulate(self):
        """
        Executes simulation by FMU in the time interval [start_time; stop_time]
        currently saved in the environment.

        :return: resulting state of the environment.
        """
        logger.debug(f'Simulation started for time interval {self.sim_time_interval[0]}-{self.sim_time_interval[1]}')

        # Advance
        x_0 = self.model.continuous_states

        # Get the output from a step of the solver
        sol_out = scipy.integrate.solve_ivp(
            self._get_deriv, self.sim_time_interval, x_0, method=self.solver_method, jac=self._calc_jac)
        # get the last solution of the solver
        self.model.continuous_states = sol_out.y[:, -1]

        obs = self.model.get_real(self.model_output_idx)
        return pd.DataFrame([obs], columns=self.model_output_names)

    @property
    def is_done(self) -> bool:
        """
        Checks if the experiment is finished using a time limit

        :return: True if simulation time exceeded
        """
        logger.debug(f't: {self.sim_time_interval[1]}, ')
        return abs(self.sim_time_interval[1]) > self.time_end

    def update_measurements(self, measurements: Union[pd.DataFrame, List[Tuple[List, pd.DataFrame]]]):
        """
        records measurements
        :type Union[pd.DataFrame, List[Tuple[List, pd.DataFrame]]
        :param measurements: measurements will be stored in an internal variable
         and the columns of the self.history is updated to be able to store the measurements as well
        :return: None
        """
        if isinstance(measurements, pd.DataFrame):
            measurements = [(measurements.colums, measurements)]
        for cols, df in measurements:
            miss_col_count = len(set(flatten(cols)) - set(self.history.cols))
            if 0 < miss_col_count < len(flatten(cols)):
                raise ValueError(
                    f'some of the columns are already added, this should not happen: cols:"{cols}";self.history.cols:"{self.history.cols}"')
            elif miss_col_count:
                self.history.cols = self.history.structured_cols() + cols

        self.__measurements = pd.concat(list(map(lambda df: df[1].reset_index(drop=True), measurements)), axis=1)

    def reset(self):
        """
        OpenAI Gym API. Restarts environment and sets it ready for experiments.
        In particular, does the following:
            * resets model
            * sets simulation start time to 0
            * sets initial parameters of the model
            * initializes the model
            * sets environment class attributes, e.g. start and stop time.
        :return: state of the environment after resetting
        """
        logger.debug("Experiment reset was called. Resetting the model.")

        self.model.reset()
        self.model.setup_experiment(start_time=0)

        self._setup_fmu()
        self.sim_time_interval = np.array([self.time_start, self.time_start + self.time_step_size])
        self.history.reset()
        self.__state = self._simulate()
        self.__measurements = pd.DataFrame()
        self.history.append(self.__state.join(self.__measurements))

        return self.__state

    def step(self, action):
        """
        OpenAI Gym API. Determines how one simulation step is performed for the environment.
        Simulation step is execution of the given action in a current state of the environment.
        :param action: action to be executed.
        :return: state, reward, is done, info
        """
        logger.debug("Experiment next step was called.")
        if self.is_done:
            logging.warning(
                """You are calling 'step()' even though this environment has already returned done = True.
                You should always call 'reset()' once you receive 'done = True' -- any further steps are
                undefined behavior.""")
            return self.__state, None, self.is_done

        # check if action is a list. If not - create list of length 1
        try:
            iter(action)
        except TypeError:
            action = [action]
            logging.warning("Model input values (action) should be passed as a list")

        # Check if number of model inputs equals number of values passed
        if len(action) != len(list(self.model_input_names)):
            message = "List of values for model inputs should be of the length {}," \
                      "equal to the number of model inputs. Actual length {}".format(
                len(list(self.model_input_names)), len(action))
            logging.error(message)
            raise ValueError(message)

        # Set input values of the model
        logger.debug("model input: {}, values: {}".format(self.model_input_names, action))
        self.model.set(list(self.model_input_names), list(action))
        if self.model_parameters:
            # list of keys and list of values
            self.model.set(*zip(*[(var, f(self.sim_time_interval[0])) for var, f in self.model_parameters.items()]))

        # Simulate and observe result state
        self.__state = self._simulate()
        obs = self.__state.join(self.__measurements)
        self.history.append(obs)

        logger.debug("model output: {}, values: {}".format(self.model_output_names, self.__state))

        # Check if experiment has finished
        # Move simulation time interval if experiment continues
        if not self.is_done:
            logger.debug("Experiment step done, experiment continues.")
            self.sim_time_interval += self.time_step_size
        else:
            logger.debug("Experiment step done, experiment done.")

        return obs, self.reward(obs), self.is_done, {}

    def render(self, mode='human', close=False):
        """
        OpenAI Gym API. Determines how current environment state should be rendered.
        Does nothing at the moment

        :param mode: rendering mode. Read more in Gym docs.
        :param close: flag if rendering procedure should be finished and resources cleaned.
        Used, when environment is closed.
        :return: rendering result
        """
        if self.viz_mode is None:
            return True
        elif close:
            if self.viz_mode == 'step':
                # TODO close plot
                pass
            else:
                for cols in self.history.structured_cols():
                    if not isinstance(cols, list):
                        cols = [cols]
                    cols = [col for col in cols if re.fullmatch(self.viz_col_regex, col)]
                    if not cols:
                        continue
                    df = self.history.df[cols].copy()
                    df.index = self.history.df.index * self.time_step_size
                    df.plot(legend=True)
                    plt.show()

        elif self.viz_mode == 'step':
            # TODO update plot
            pass
        return True

    def close(self):
        """
        OpenAI Gym API. Closes environment and all related resources.
        Closes rendering.
        :return: True on success
        """
        return self.render(close=True)
