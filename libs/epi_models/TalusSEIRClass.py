"""Implementation of an SEIR compartment model with R = Recovered + Deceased,
I = I1 + I2 + I3 (increasing severity of infection), with an asymptomatic
infectious compartment (A).
"""

# standard modules
import datetime
import logging
from copy import deepcopy

# 3rd party modules
import numpy as np
import pandas as pd
from scipy.integrate import odeint

_logger = logging.getLogger(__name__)


class EpiRun:
    def __init__(self, type, model_run):
        self.initlization_time = datetime.datetime.now()
        self.type = type  # base or intervention
        self.start_date = model_run.start_date
        self.model_run = model_run

    def generate_epi_params(self):
        self.EpiParams = self.EpiParams(self.model_run)

    def generate_initial_conditions(self):
        self.InitConditions = self.TimeStep(self.model_run)

    class TimeStep:
        def __init__(self, model_run):
            self.N = model_run.population

            # this is an inital run, will have to build the initial conditions from the
            # timeseries data
            self.hospitalized = model_run.past_data.get(key="cases", default=0) / 4
            self.mild = self.hospitalized / model_run.hospitalization_rate
            self.icu = (
                self.hospitalized * model_run.hospitalized_cases_requiring_icu_care
            )
            self.asymp = self.mild * (
                model_run.percent_asymp / 1 - model_run.percent_asymp
            )
            self.dead = model_run.past_data.get(key="deaths", default=0)

            self.exposed = model_run.exposed_infected_ratio * self.mild

            self.infected = self.asymp + self.mild + self.hospitalized + self.icu

            self.recovered = model_run.past_data.get(key="recovered", default=0)

            susceptible = self.N - (self.infected + self.recovered + self.dead)

            self.y0 = [
                int(self.exposed),
                int(self.mild),
                int(self.hospitalized),
                int(self.icu),
                int(self.recovered),
                int(self.dead),
                int(self.asymp),
            ]

    class EpiParams:
        def __init__(self, model_run):

            self.N = model_run.population

            self.fraction_critical = (
                (1 - model_run.percent_asymp)
                * model_run.hospitalization_rate
                * model_run.hospitalized_cases_requiring_icu_care
            )

            self.alpha = 1 / model_run.presymptomatic_period

            self.beta = (
                0,
                model_run.beta / self.N,
                model_run.beta_hospitalized / self.N,
                model_run.beta_icu / self.N,
                # TODO move beta.A to model params
                model_run.beta / self.N,
                # A = 0,
            )

            self.beta_A = model_run.beta / self.N

            # have to calculate these in order and then put them into arrays
            self.gamma_0 = 0
            self.gamma_1 = (1 / model_run.duration_mild_infections) * (
                1 - model_run.hospitalization_rate
            )

            self.rho_0 = 0
            self.rho_1 = (1 / model_run.duration_mild_infections) - self.gamma_1
            self.rho_2 = (1 / model_run.hospital_time_recovery) * (
                self.fraction_critical / model_run.hospitalization_rate
            )

            self.gamma_2 = (1 / model_run.hospital_time_recovery) - self.rho_2

            self.mu = (1 / model_run.icu_time_death) * (
                model_run.case_fatality_rate / self.fraction_critical
            )
            self.gamma_3 = (1 / model_run.icu_time_death) - self.mu

            # TODO move gamma_a to model params
            self.gamma_A = self.gamma_1

            self.gamma = (
                self.gamma_0,
                self.gamma_1,
                self.gamma_2,
                self.gamma_3,
                self.gamma_A,
            )
            # "gamma": L(gamma_0, gamma_1, gamma_2, gamma_3, A = 0),
            self.rho = [self.rho_0, self.rho_1, self.rho_2]
            self.f = model_run.percent_asymp

        # TODO update to match latest model:
        # R0 = N*((1-f)*BA/gA + f*((B1/(p1+g1))+(p1/(p1+g1))*(B2/(p2+g2)+ (p2/(p2+g2))*(B3/(m+g3)))))
        def generate_r0(self):
            """Short summary.

            Parameters
            ----------
            seir_params : type
                Description of parameter `seir_params`.
            N : type
                Description of parameter `N`.

            Returns
            -------
            type
                Description of returned object.

            """
            b = self.beta
            p = self.rho
            g = self.gamma
            u = self.mu

            r0 = self.N * (
                (b[1] / (p[1] + g[1]))
                + (p[1] / (p[1] + g[1]))
                * (b[2] / (p[2] + g[2]) + (p[2] / (p[2] + g[2])) * (b[3] / (u + g[3])))
            )

            return r0

    def dataframe_ify(self):
        """Return human-friendly dataframe of model results.

        Parameters
        ----------
        data : type
            Description of parameter `data`.
        start : type
            Description of parameter `start`.
        end : type
            Description of parameter `end`.
        steps : type
            Description of parameter `steps`.

        Returns
        -------
        type
            Description of returned object.

        """
        self.last_period = self.start_date + datetime.timedelta(days=(self.steps - 1))

        timesteps = pd.date_range(
            # start=start, end=last_period, periods=steps, freq=='D',
            start=self.start_date,
            end=self.last_period,
            freq="D",
        ).to_list()

        data = self.results

        seir_df = pd.DataFrame(
            zip(data[0], data[1], data[2], data[3], data[4], data[5], data[6]),
            columns=[
                "exposed",
                "infected_a",
                "infected_b",
                "infected_c",
                "recovered",
                "dead",
                "asymp",
            ],
            index=timesteps,
        )

        # reample the values to be daily
        seir_df.resample("1D").sum()

        # drop anything after the end day
        seir_df = seir_df.loc[: self.last_period]

        self.results_df = seir_df

        return

    # The SEIR model differential equations.
    # https://github.com/alsnhll/SEIR_COVID19/blob/master/SEIR_COVID19.ipynb
    # but these are the basics
    # y = initial conditions
    # t = a grid of time points (in days) - not currently used, but will be for time-dependent functions
    # N = total pop
    # beta = contact rate
    # gamma = mean recovery rate
    # Don't track S because all variables must add up to 1
    # include blank first entry in vector for beta, gamma, p so that indices align in equations and code.
    # In the future could include recovery or infection from the exposed class (asymptomatics)
    def deriv(self, y0, t):
        """Calculate and return the current values of dE/dt, etc. for each model
        compartment as numerical integration is performed. This function is the
        first argument of the odeint numerical integrator function.

        Parameters
        ----------
        y0 : type
            Description of parameter `y0`.
        t : type
            Description of parameter `t`.

        Returns
        -------
        type


        """
        # S = N - sum(y0)
        S = np.max([self.model_run.population - sum(y0), 0])

        E = y0[0]
        I1 = y0[1]
        I2 = y0[2]
        I3 = y0[3]
        R = y0[4]
        D = y0[5]
        A = y0[6]

        I_all = [I1, I2, I3]
        I_transmission = np.dot(self.EpiParams.beta[1:4], I_all)
        I_recovery = np.dot(self.EpiParams.gamma[1:4], I_all)
        A_transmission = A * self.EpiParams.beta_A
        A_recovery = A * self.EpiParams.gamma_A
        all_infected = sum(I_all) + A

        dE = np.min([(A_transmission + I_transmission) * S, S]) - (
            self.EpiParams.alpha * E
        )  # Exposed
        dA = ((1 - self.EpiParams.f) * self.EpiParams.alpha * E) - (
            self.EpiParams.gamma_A * A
        )  # asymp
        dI1 = (self.EpiParams.f * self.EpiParams.alpha * E) - (
            self.EpiParams.gamma[1] + self.EpiParams.rho[1]
        ) * I1  # Ia - Mildly ill
        dI2 = (self.EpiParams.rho[1] * I1) - (
            self.EpiParams.gamma[2] + self.EpiParams.rho[2]
        ) * I2  # Ib - Hospitalized
        dI3 = (self.EpiParams.rho[2] * I2) - (
            (self.EpiParams.gamma[3] + self.EpiParams.mu) * I3
        )  # Ic - ICU
        dR = np.min([A_recovery + I_recovery, all_infected])  # Recovered
        dD = self.EpiParams.mu * I3  # Deaths

        dy = [dE, dI1, dI2, dI3, dR, dD, dA]
        return dy

    # Sets up and runs the integration
    # start date and end date give the bounds of the simulation
    # pop_dict contains the initial populations
    # beta = contact rate
    # gamma = mean recovery rate
    # TODO: add other params from doc
    def seir(self):
        self.steps = self.model_run.days_to_model
        t = np.arange(0, self.steps, 1)

        y0 = [
            self.InitConditions.exposed,
            self.InitConditions.mild,
            self.InitConditions.hospitalized,
            self.InitConditions.icu,
            self.InitConditions.recovered,
            self.InitConditions.dead,
            self.InitConditions.asymp,
        ]

        ret = odeint(self.deriv, y0, t, args=())

        self.results = np.transpose(ret)

        return


class Intervention(EpiRun):
    def __init__(self, intervention, model_run):
        # get the base run done so we have a place to start
        self.type = "intervention"
        self.model_run = model_run
        self.intervention = intervention
        super(Intervention, self).__init__(self.type, self.model_run)
        self.start_date = intervention["start_date"]
        self.get_new_init_conditions()
        self.get_new_epi_params()

        self.seir()
        self.dataframe_ify()

        ### todo: change params
        # self.EpiParams = super().EpiParams(model_run)

    def get_new_init_conditions(self):
        self.model_run.past_data = self.intervention["initial_conditions"]

        self.InitConditions = self.TimeStep(self.model_run)

    def get_new_epi_params(self):
        for param_name, param_value in self.intervention["new_parameters"].items():
            if param_name == "r0":
                self.model_run.beta = self.brute_force_r0(param_value)
            else:
                setattr(self.model_run, param_name, param_value)

        self.EpiParams = super().EpiParams(self.model_run)

    class TimeStep:
        def __init__(self, model_run):
            self.N = model_run.population

            # this should be an intervention run, so the initial conditions are more
            # fleshed out
            self.mild = model_run.past_data.get(key="infected_a", default=0)
            self.hospitalized = model_run.past_data.get(key="infected_b", default=0)
            self.icu = model_run.past_data.get(key="infected_c", default=0)
            self.asymp = model_run.past_data.get(key="asymp", default=0)
            self.dead = model_run.past_data.get(key="dead", default=0)

            self.exposed = model_run.exposed_infected_ratio * self.mild

            self.infected = self.asymp + self.mild + self.hospitalized + self.icu

            self.recovered = model_run.past_data.get(key="recovered", default=0)

            susceptible = self.N - (self.infected + self.recovered + self.dead)

            self.y0 = [
                int(self.exposed),
                int(self.mild),
                int(self.hospitalized),
                int(self.icu),
                int(self.recovered),
                int(self.dead),
                int(self.asymp),
            ]

    def brute_force_r0(self, new_r0):
        """This function will be obsolete when the procedure for introducing
        interventions into model runs is updated -- do not maintain it.

        Parameters
        ----------
        seir_params : type
            Description of parameter `seir_params`.
        new_r0 : type
            Description of parameter `new_r0`.
        r0 : type
            Description of parameter `r0`.
        N : type
            Description of parameter `N`.

        Returns
        -------
        type
            Description of returned object.

        """
        calc_r0 = self.r0

        change = np.sign(new_r0 - calc_r0) * 0.00005
        # step = 0.1
        # direction = 1 if change > 0 else -1

        NewEpiParams = self.EpiParams.deepcopy()

        while round(new_r0, 4) != round(calc_r0, 4):
            NewEpiParams["beta"] = [
                0.0,
                NewEpiParams["beta"][1] + change,
                NewEpiParams["beta"][2],
                NewEpiParams["beta"][3],
            ]
            calc_r0 = generate_r0(new_seir_params, N)

            diff_r0 = new_r0 - calc_r0

            # if the sign has changed, we overshot, turn around with a smaller
            # step
            if np.sign(diff_r0) != np.sign(change):
                change = -change / 2

        new_seir_params["beta"] = L(new_seir_params["beta"])

        return new_seir_params
