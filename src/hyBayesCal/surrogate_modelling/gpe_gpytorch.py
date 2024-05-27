"""
GPyTorch library for GP training
"""
import numpy as np
import sys
import sklearn
import scipy
import copy
from joblib import Parallel, delayed
import torch
import gpytorch
from scipy.optimize import dual_annealing, differential_evolution


class MyExactGPyModel(gpytorch.models.ExactGP):
    """
    Instance of GPyTorch's "ExactGP" library, with custom likelihood, kernel, training points.

    The likelihood is kept constant: Gaussian Likelihood (https://docs.gpytorch.ai/en/latest/likelihoods.html)

    Parameters:
        :param train_x: <np.array[n_tp, n_p]> with parameter sets used to train GPR
        :param train_y: <np.array[n_tp, n_obs]> with forward model outputs used to trian GPR
        :param kernel: <kernel instance> with kernel used in GPR
        :param likelihood <likelihood instance> to train noise in GPR
    """

    def __init__(self, train_x, train_y, kernel, likelihood):
        super(MyExactGPyModel, self).__init__(train_inputs=train_x, train_targets=train_y, likelihood=likelihood)
        # self.mean_module = gpytorch.means.ConstantMean()
        self.mean_module = gpytorch.means.ZeroMean()
        self.covar_module = kernel

    def forward(self, x):
        """
        Takes in the training data (x) and returns a multivariate normal distribution with mean and covariance (kernel)
        set in "__init__()"
        :param x: training data (parameter sets)
        :return:
        """
        mean_x = self.mean_module(x)
        covar_x = self.covar_module(x)
        return gpytorch.distributions.MultivariateNormal(mean_x, covar_x)


class GPyTraining:
    """
    Uses the 'GPyTorch' ExactGPR library to generate a GPE for a given forward model, based on collocation points
    generated by said forward model.

    Parameters:
        :param collocation_points = np.array(n_tp, n_p),
            with training points (parameter sets)
        :param model_evaluations = np.array(nn_tp, n_obs),
            model outputs in each location where the fcm was evaluated
        :param y_normalization: <bool> True to normalize model outputs before training, False to train as is
        :param tp_normalization: bool, False (default) to use training points as they are, True to normalize TP
        parameter values before training GPE

        :param parallelize : bool
            True to parallelize surrogate training, False to train in a sequential loop

    Parameters needed by GPyTorch for GPR:
    (more info: )
        :param kernel = <instance from "gpytorch.kernels">
            to be used to train GPE. Can be sent with the default values or with user-defined isotropy
            (ard_num_dims=number of parameters)...
        :param likelihood = <instance from "gpytorch.likelihoods.GaussianLikelihood">
            used for the optimization of the GPR using the PyTorch library. Can include the default
            constrains/initial values or the user can change these in the main() file.
        :param training_iter = <int>
            number of optimizer iterations to train GPE
        :param optimizer: <str>
            with name of optimizer to use : 'adam' (default) or 'lbfgs'
        :param loss: <str>
            with name of loss function to use. Either 'exact' or 'loo' are available at the moment
        :param n_restarts: <int>
            Number of optimization restarts
        :param tp_normalization = <bool>
            NOT USED YET
        :param y_normalization = <bool>,
            True to normalize output values, which means the predictions also need to be de-normalized afterwards.

    TODO: Give evaluation location as input and then, add a function receives the observation point location and extracts
     the gpe predictions from it. These are the ones that will be used in BAL.
    TODO: For GPyTorch, check the GPU settings (if needed) and other gpytorch.settings to predict values.
    """

    def __init__(self, collocation_points, model_evaluations, kernel, training_iter, likelihood,
                 y_normalization=True, tp_normalization=False, optimizer="adam", lr=0.5,
                 loss='exact', n_restarts=1, weight_decay=0,
                 gradient_free_start=False,
                 verbose=True,
                 parallelize=False):

        # Basic attributed
        self.training_points = collocation_points
        self.model_evaluations = model_evaluations

        self.n_obs = self.model_evaluations.shape[1]
        self.n_params = collocation_points.shape[1]

        self.gp_list = []

        # Input for GPR library in GPyTorch:
        self.kernel = kernel
        self.optimizer_ = optimizer
        self.training_iter = training_iter
        self.loss = loss
        self.n_restarts = n_restarts
        self.gradient_free_start = gradient_free_start
        self.lr = lr
        self.weight_decay = weight_decay

        # self.likelihood = likelihood

        self.parallel = parallelize

        self.verbose = verbose

        # Options for GPR library:
        self.tp_norm = tp_normalization
        self.y_norm = y_normalization

        self._id_vectors(likelihood, kernel)

    def _id_vectors(self, likelihood, kernel):
        """
        Function checks if the inputs for alpha and kernel are a single variable or a list. If they are a single value,
        the function generates a list filled with the same value/object, so it can be properly read in the train_
        function.
        Args:
            likelihood: <object> or <list of objects [n_obs]>
                with input alpha value(s). If list, there should be one value per observation.
            kernel: <object> or <list of objects [n_obs]>
                Scikit learn kernel objects, to send to the GPR training

        Returns:
        """
        if isinstance(likelihood, list):
            self.likelihood = np.array(likelihood)
        else:
            self.likelihood = np.full(self.n_obs, likelihood)

        # if isinstance(kernel, list):
        #     self.kernel = kernel
        # else:
        #     self.kernel = np.full(self.n_obs, kernel)

    @staticmethod
    def convert_to_tensor(array):
        """
        Function to transform np.array to a tensor
        Args:
            array: <np.array> that you want to change to a tensor

        Returns: <tensor> data in np.array transformed to tensor format
        """
        transformed = torch.tensor(array).float()
        return transformed

    def normalize_tp(self, train_y):
        """
        Function to normalize training points outputs before training
        Args:
            train_y: <np.array[tp_size, n_obs]> with model output values to normalize

        Returns: <tensor> with normalized input values.
        """
        norm_y = (train_y - np.mean(train_y))/(np.std(train_y))
        train_y = self.convert_to_tensor(norm_y)
        return train_y

    def train_(self):
        """
        Function trains the surrogate model using the GPyTorch library, using the given optimizer.
        Returns:
        ToDo: parallelize training
        """
        # Convert training points and prior parameter sets to tensor:
        train_x = self.convert_to_tensor(self.training_points)

        if self.parallel and self.n_obs > 1:
            out = Parallel(n_jobs=-1, backend='multiprocessing')(delayed(self._fit_adam)(train_x=train_x,
                                                                                         model_y=self.model_evaluations[:, i],
                                                                                         likelihood=self.likelihood[i],
                                                                                         kernel=self.kernel)
                                                                 for i in range(self.n_obs))
            self.gp_list = out
        else:
            for i, y_model in enumerate(self.model_evaluations.T):
                out = self._fit_adam(train_x=train_x, model_y=y_model, likelihood=self.likelihood[i],
                                     kernel=self.kernel)

                self.gp_list.append(out)

    @staticmethod
    def init_model_params(model):
        """
        Function to initalize model hyperparameters, for multi-start optimizations
        Args:
            model: GPyTorch instance
        Returns:
        """
        def initialize_tensor(param_):
            if len(param_.shape) < 2:
                # If the tensor has fewer than two dimensions, apply a different initialization method
                torch.nn.init.uniform_(param_)  # Example: Uniform initialization for tensors with fewer than 2 dimensions
            else:
                torch.nn.init.xavier_uniform_(param_)

        for name, param in model.named_parameters():
            initialize_tensor(param)

    def _fit_adam(self, train_x, model_y, kernel, likelihood):
        """
        Function trains the GPR for a given training location using the 'adam' optimizer from PyTorch
        Args:
            train_x: tensor [n_tp, n_param]
                with input parameter sets to use in training
            model_y: array[n_tp,]
                with simulator outputs in training points
        Returns: dict
            with trained gp object, trained likelihood object, hyperparameters
            and y_normalization parameters (if needed)

        """
        # 0. Normalize, if needed, and transform model_evaluations at loc "i" to a tensor
        if self.y_norm:
            train_y = self.normalize_tp(model_y)
        else:
            train_y = self.convert_to_tensor(model_y)

        best_loss = float('inf')
        best_params = None

        for i in range(self.n_restarts):

            # 1.Initialize kernel and likelihood:
            kernel_ = copy.deepcopy(kernel)
            likelihood_ = copy.deepcopy(likelihood)

            # 2.Initialize instance of GPyTorch GPR:
            gp = MyExactGPyModel(train_x, train_y, kernel_, likelihood_)

            # Start training
            gp.train()
            likelihood_.train()

            if i > 1:
                self.init_model_params(gp)

            # Setup Optimizer
            if self.optimizer_ == 'adam':
                optimizer = torch.optim.Adam(gp.parameters(), lr=self.lr, weight_decay=self.weight_decay)
            elif self.optimizer_ == 'lbfgs':
                optimizer = torch.optim.lbfgs(gp.parameters(), lr=self.lr)
            else:
                sys.exit(f'There is no optimizer {self.optimizer_} available.')

            # Loss for GPs - log likelihood
            if 'exact' in self.loss.lower():
                mll = gpytorch.mlls.ExactMarginalLogLikelihood(likelihood_, gp)
            elif 'loo' in self.loss.lower():
                mll = gpytorch.mlls.LeaveOneOutPseudoLikelihood(likelihood_, gp)

            # Change learning rate:
            scheduler = torch.optim.lr_scheduler.StepLR(optimizer,
                                                        step_size=5,
                                                        gamma=0.5)

            if i == 0 and self.gradient_free_start:
                def negative_log_likelihood(params):
                    """ Gradient-free optimizer from SciPy"""
                    # Set the model hyperparameters based on the optimization parameters
                    gp.covar_module.base_kernel.lengthscale = torch.tensor(params[0:self.n_params])
                    gp.covar_module.outputscale = torch.tensor(params[-2])
                    gp.likelihood.noise = torch.tensor(params[-1])

                    # Zero out the gradients
                    gp.zero_grad()

                    # Forward pass to compute the negative log likelihood
                    output = gp(train_x)
                    loss = -mll(output, train_y)

                    if self.verbose:
                        print(f'Gradient-free start - Loss: {loss.item()}',
                              f'   Outputscale: {gp.covar_module.outputscale.item()}',
                              f'   lengthscale: {gp.covar_module.base_kernel.lengthscale[0]}',
                              f'   noise: {gp.likelihood.noise.item()}')

                    # Return the negative log likelihood as a NumPy array
                    return loss.item()

                # Define the bounds for the optimization parameters (lengthscale and outputscale)
                value = np.empty((), dtype=object)
                value[()] = (1e-3, 1e2)
                bounds_1 = list(np.full(self.n_params, value, dtype=object))
                bounds_2 = [(1e-2, 1e1), (1e-6, 1)]
                bounds = bounds_1 + bounds_2

                # Perform the global optimization using differential evolution
                # t1 = time.time()
                result = differential_evolution(negative_log_likelihood, bounds, maxiter=10)

            # Optimize parameters:
            for j in range(self.training_iter):
                # a. Zero gradients from previous iteration
                optimizer.zero_grad()
                # b. Output from model
                output = gp(train_x)
                # c. Calculate loss and back propagation gradients
                loss = -mll(output, train_y)
                loss.backward()
                optimizer.step()
                scheduler.step()   # Change learning rate
                if self.verbose:
                    print(f'Iter {j + 1}/{self.training_iter} - Loss: {loss.item()}',
                          f'   Outputscale: {gp.covar_module.outputscale.item()}',
                          f'   lengthscale: {gp.covar_module.base_kernel.lengthscale[0]}',
                          f'   noise: {gp.likelihood.noise.item()}')
            if loss < best_loss:
                best_loss = loss
                best_params = gp.state_dict()

        gp.load_state_dict(best_params)

        with torch.no_grad():
            return_out_dic = dict()
            return_out_dic['gp'] = gp
            return_out_dic['likelihood'] = likelihood_

            return_out_dic['c_hp'] = gp.covar_module.outputscale.item()
            return_out_dic['cl_hp'] = gp.covar_module.base_kernel.lengthscale.numpy()[0, :]
            return_out_dic['noise_hp'] = gp.likelihood.noise.item()
            if self.y_norm:
                return_out_dic['y_norm'] = [np.mean(model_y), np.std(model_y)]

        return return_out_dic

    # def _fit_lbfgs(self, train_x, model_y, kernel, likelihood):
    #     """
    #     Function trains the GPR for a given training location using an external 'lbfgs' optimizer
    #     Source: (https://github.com/hjmshi/PyTorch-LBFGS)
    #     Args:
    #         train_x: tensor [n_tp, n_param]
    #             with input parameter sets to use in training
    #         model_y: array [n_tp, ]
    #             with simulator outputs, corresponding to train_x, to use in training
    #
    #     Returns: dict
    #         with trained gp object, trained likelihood object, hyperparameters
    #         and y_normalization parameters (if needed)
    #
    #     """
    #
    #     # 0. Normalize, if needed, and transform model_evaluations at loc "i" to a tensor
    #     if self.y_norm:
    #         train_y = self.normalize_tp(model_y)
    #     else:
    #         train_y = self.convert_to_tensor(model_y)
    #
    #     # 1.Initialize kernel and likelihood:
    #     kernel_ = copy.deepcopy(kernel)
    #     likelihood_ = copy.deepcopy(likelihood)
    #
    #     # 2.Initialize instance of GPyTorch GPR:
    #     gp = MyExactGPyModel(train_x, train_y, kernel_, likelihood_)
    #
    #     # Start training
    #     gp.train()
    #     likelihood_.train()
    #
    #     def closure():
    #         # a. Zero gradients from previous iteration
    #         optimizer.zero_grad()
    #         # b. Output from model
    #         output = gp(train_x)
    #         loss = -mll(output, train_y)
    #         return loss
    #
    #     # 4.1 Optimize parameters:
    #
    #     # Setup Optimizer
    #     optimizer = FullBatchLBFGS(gp.parameters(), debug=False, lr=0.01)
    #
    #     # Loss for GPs - log likelihood
    #     mll = gpytorch.mlls.ExactMarginalLogLikelihood(likelihood_, gp)
    #
    #     loss = closure()
    #     loss.backward()
    #
    #     # Perform step and update curvature:
    #     for j in range(self.training_iter):
    #         # perform step and update curvature
    #         options = {'closure': closure, 'current_loss': loss, 'max_ls': 30}  # , 'inplace': False}
    #         loss, _, lr, _, F_eval, G_eval, _, _ = optimizer.step(options)
    #
    #         with torch.no_grad():
    #             if self.verbose:
    #                 ls = gp.covar_module.base_kernel.lengthscale.numpy()[0, :]
    #                 print(f'Iteration {j + 1}/{self.training_iter}  -  Loss: {loss.item()}  -  LR: {lr}',
    #                       f'  -  Func.Evals: {F_eval}  -  grad.Evals: {G_eval}  -  Lengthscales: [{ls}]',
    #                       f'  -  Noise: {gp.likelihood.noise.item()}')
    #
    #     with torch.no_grad():
    #         return_out_dic = dict()
    #         return_out_dic['gp'] = gp
    #         return_out_dic['likelihood'] = likelihood_
    #
    #         return_out_dic['c_hp'] = gp.covar_module.outputscale.item()
    #         return_out_dic['cl_hp'] = gp.covar_module.base_kernel.lengthscale.numpy()[0, :]
    #         try:
    #             return_out_dic['noise_hp'] = gp.likelihood.noise.item()
    #         except:
    #             return_out_dic['noise_hp'] = gp.likelihood.noise.numpy()
    #         if self.y_norm:
    #             return_out_dic['y_norm'] = [np.mean(model_y), np.std(model_y)]
    #
    #     return return_out_dic

    def predict_(self, input_sets, get_conf_int=False):

        prior_x = self.convert_to_tensor(input_sets)

        # surrogate_prediction = np.zeros((len(self.gp_list), input_sets.shape[0]))  # GPE mean, for each obs
        # surrogate_std = np.zeros((len(self.gp_list), input_sets.shape[0]))  # GPE mean, for each obs
        surrogate_prediction = np.zeros((input_sets.shape[0], len(self.gp_list)))  # GPE mean, for each obs
        surrogate_std = np.zeros((input_sets.shape[0], len(self.gp_list)))  # GPE mean, for each obs
        if get_conf_int:
            # upper_ci = np.zeros((len(self.gp_list), input_sets.shape[0]))  # GPE mean, for each obs
            # lower_ci = np.zeros((len(self.gp_list), input_sets.shape[0]))  # GPE mean, for each obs
            upper_ci = np.zeros((input_sets.shape[0], len(self.gp_list)))  # GPE mean, for each obs
            lower_ci = np.zeros((input_sets.shape[0], len(self.gp_list)))  # GPE mean, for each obs

        for i in range(0, self.n_obs):
            # Extract data
            gp = copy.deepcopy(self.gp_list[i]['gp'])
            likelihood_ = copy.deepcopy(self.gp_list[i]['likelihood'])

            # 5.1 Go into eval mode:
            gp.eval()
            likelihood_.eval()

            with torch.no_grad():
                f_pred = likelihood_(gp(prior_x))
                prediction = f_pred.mean.numpy()
                std = f_pred.stddev.numpy()
                if self.y_norm:  # Back-transform
                    # normalized results
                    prediction = self.gp_list[i]['y_norm'][1] * prediction + self.gp_list[i]['y_norm'][0]
                    std = std * self.gp_list[i]['y_norm'][1]

                surrogate_prediction[:, i] = prediction
                surrogate_std[:, i] = std

                # Calculate 95% confidence intervals.
                if get_conf_int:
                    upper_ci[:, i] = surrogate_prediction[:, i] + 2 * surrogate_std[:, i]
                    lower_ci[:, i] = surrogate_prediction[:, i] - 2 * surrogate_std[:, i]

        output_dic = dict()
        output_dic['output'] = surrogate_prediction
        output_dic['std'] = surrogate_std
        if get_conf_int:
            output_dic['upper_ci'] = upper_ci
            output_dic['lower_ci'] = lower_ci

        return output_dic


def validation_error(true_y, sim_y, output_names, n_per_type):
    """
    Estimates different evaluation (validation) criteria for a surrogate model, for each output location. Results for
    each output type are saved under different keys in a dictionary.
    Args:
        true_y: array [mc_valid, n_obs]
            simulator outputs for valid_samples
        sim_y: array [mc_valid, n_obs] or dict{}
            surrogate/emulator's outputs for valid_samples. If a dict is given, it has output and std keys.
        output_names: array [n_types,]
            with strings, with name of each output
        n_per_type: int
            Number of observation per output type

    Returns: float, float or array[n_obs], float or array[n_obs]
        with validation criteria for each output locaiton, and each output type

    ToDo: Like in BayesValidRox, estimate surrogate predictions here, by giving a surrogate object as input (maybe)
    ToDo: add as part of MyGeneralGPR class, and the outputs are a dictionary, with output type as a key.
    """
    criteria_dict = {'rmse': dict(),
                     'mse': dict(),
                     'nse': dict(),
                     'r2': dict(),
                     'mean_error': dict(),
                     'std_error': dict()}

    # criteria_dict = {'rmse': dict(),
    #                  'valid_error': dict(),
    #                  'nse': dict()}

    if isinstance(sim_y, dict):
        sm_out = sim_y['output']
        sm_std = sim_y['std']
        upper_ci = sim_y['upper_ci']
        lower_ci = sim_y['lower_ci']

        criteria_dict['norm_error'] = dict()
        criteria_dict['P95'] = dict()
    else:
        sm_out = sim_y

    # RMSE for each output location: not a dictionary (yet). [n_obs, ]
    rmse = sklearn.metrics.mean_squared_error(y_true=true_y, y_pred=sm_out, multioutput='raw_values',
                                              squared=False)

    c = 0
    for i, key in enumerate(output_names):
        # RMSE
        criteria_dict['rmse'][key] = sklearn.metrics.mean_squared_error(y_true=true_y[:, c:c + n_per_type],
                                                                        y_pred=sm_out[:, c:c + n_per_type],
                                                                        multioutput='raw_values', squared=False)
        # # NSE
        criteria_dict['nse'][key] = sklearn.metrics.r2_score(y_true=true_y[:, c:c+n_per_type],
                                                             y_pred=sm_out[:, c:c+n_per_type],
                                                             multioutput='raw_values')
        # # Validation error:
        # criteria_dict['valid_error'][key] = criteria_dict['rmse'][key] ** 2 / np.var(true_y[:, c:c+n_per_type],
        #                                                                              ddof=1, axis=0)

        # NSE
        criteria_dict['nse'][key] = sklearn.metrics.r2_score(y_true=true_y[:, c:c + n_per_type],
                                                             y_pred=sm_out[:, c:c + n_per_type],
                                                             multioutput='raw_values')
        criteria_dict['mse'][key] = sklearn.metrics.mean_squared_error(y_true=true_y[:, c:c + n_per_type],
                                                                       y_pred=sm_out[:, c:c + n_per_type],
                                                                       multioutput='raw_values', squared=True)
        # Mean errors
        criteria_dict['mean_error'][key] = np.abs(
            np.mean(true_y[:, c:c + n_per_type], axis=0) - np.mean(sm_out[:, c:c + n_per_type], axis=0)) / np.mean(
            true_y[:, c:c + n_per_type], axis=0)

        criteria_dict['std_error'][key] = np.abs(
            np.std(true_y[:, c:c + n_per_type], axis=0) - np.std(sm_out[:, c:c + n_per_type], axis=0)) / np.std(
            true_y[:, c:c + n_per_type], axis=0)

        # Norm error
        if isinstance(sim_y, dict):
            # Normalized error
            ind_val = np.divide(np.subtract(sm_out[:, c:c + n_per_type], true_y[:, c:c + n_per_type]),
                                sm_std[:, c:c + n_per_type])
            criteria_dict['norm_error'][key] = np.mean(ind_val ** 2, axis=0)

            # P95
            p95 = np.where((true_y[:, c:c + n_per_type] <= upper_ci[:, c:c + n_per_type]) & (
                        true_y[:, c:c + n_per_type] >= lower_ci[:, c:c + n_per_type]), 1, 0)
            criteria_dict['P95'][key] = np.mean(p95, axis=0)

        criteria_dict['r2'][key] = np.zeros(n_per_type)
        for j in range(n_per_type):
            criteria_dict['r2'][key][j] = np.corrcoef(true_y[:, j+c], sm_out[:, j+c])[0, 1]

        c = c + n_per_type

    return rmse, criteria_dict


def save_valid_criteria(new_dict, old_dict, n_tp):
    """
    Saves the validation criteria for the current iteration (n_tp) to an existing dictionary, so we can have the
    results for all iterations in the same file. Each dictionary has a dictionary for each validation criteria.
    Each validation criteria has a key for each output type, which corresponds to a vector with n_loc, one value for
    each output value.
    Args:
        new_dict: Dict
            with the validation criteria for the current iteration
        old_dict: Dict
            With the validation criteria for all the previous iterations, including a key for N_tp, which saves
            the number of iteration.
        n_tp: int
            number of training points for the current BAL iteration.

    Returns: dict, with the old dictionary, with the
    """

    if len(old_dict) == 0:
        old_dict = dict(new_dict)
        old_dict['N_tp'] = [n_tp]
    else:
        for key in old_dict:
            if key == 'N_tp':
                old_dict[key].append(n_tp)
            else:
                for out_type in old_dict[key]:
                    old_dict[key][out_type] = np.vstack((old_dict[key][out_type], new_dict[key][out_type]))

    return old_dict
