import torch
import torch.nn as nn
from torch.utils.data import random_split, Dataset, Subset
from typing import List, Tuple, Union
from ..utils.data import MultiLabelledDataset
from ..utils.simple_models import LinearModel
from tqdm import tqdm

class ReferenceClass(nn.Module):
    """
    Conditional Classification for Any Finite Classes
    """
    def __init__(
            self,
            prev_header: str,
            dataset: MultiLabelledDataset,
            subset_fracs: List[float],
            num_iter: int, 
            lr: float,
            device: torch.device = torch.device('cpu')
    ):
        """
        Initialize the conditional learner for finite class classification.
        Compute the learning rate of PSGD for the given lr coefficient using
        the formula:
            beta = O(sqrt(1/num_iter * dim_sample)).

        Parameters:
        prev_header (str):              The header of the previous module.
        dataset (MultiLabelledDataset): For paralel processing, the dataset with mapped labels. Note that the dataset is 
                                        multi-labelled that each label is mapped to multiple errors, each of which is 
                                        generated by a corresponding predictor. 
                                        labels:   [num train sample, num predictors]
                                        features: [num train sample, dim sample]
        num_iter (int):                 The number of iterations for optimizer.
        lr (float):                     The learning rate.
        subset_fracs (List[float]):     The ratio between training data size and validation data size.
        device (torch.device):          The device to be used.
        """
        super(ReferenceClass, self).__init__()
        self.header = " ".join([prev_header, "learning reference class", "-"])
        self.num_iter = num_iter
        self.lr = lr    
        self.device = device

        print(f"{self.header} training dataset feature size: {(len(dataset), dataset.dim_feature())}")
        print(f"{self.header} training dataset label size: {(len(dataset), dataset.dim_label())}")

        if sum(subset_fracs) > 1:
            raise ValueError(f"{self.header} sum of fractions of subsets exceed 1.")
        
        # generate the training and validation datasets
        subset_sizes = [int(len(dataset) * frac) for frac in subset_fracs]

        if len(subset_sizes) == 1:
            self.dataset_val = None
            self.dataset_train, self.dataset_eval = random_split(
                dataset, 
                subset_sizes + [len(dataset) - sum(subset_sizes)],
                # generator=torch.Generator().manual_seed(42)
            )
        elif len(subset_sizes) == 2:
            self.dataset_train, self.dataset_val, self.dataset_eval = random_split(
                dataset, 
                subset_sizes + [len(dataset) - sum(subset_sizes)],
                # generator=torch.Generator().manual_seed(42)
            )
        else:
            raise ValueError(f"{self.header} Invalid number of subset sizes.")

    def forward(
            self, 
            observations: torch.Tensor          # [num observations, num predictors, dim sample]
    ) -> Tuple[torch.Tensor, torch.Tensor, LinearModel]:
        """
        Call optimizer for the sparse predictors using all the data given.
        
        Note that the optimizer runs in parallel for all the sparse predictors.
        PSGD optimizer will return one selector for each sparse predictor.

        For each cluster, we evaluate the best classifier-selector pair using all the data given
        due to insufficient data size.

        At last, we use the same data set to find the best classifier-selector pair across cluster.

        Parameters:
        observations (torch.Tensor):    The observations for initialization the optimizer.
                                        [num observations, num predictors, dim sample]

        Returns:
        min_val (torch.Tensor):         The minimum error rate.         [num observations]
        min_ids (torch.Tensor):         The minimum error rate indices. [num observations]
        selectors (LinearModel):        The selector model.             [num observations, num predictors, dim sample]
        """        

        # initialize progress bar to count converged weights
        self.converged_bar = tqdm(
            total=observations.size(0) * observations.size(1),
            desc=f"{self.header} converging"
        )

        # call optimizer
        selectors: LinearModel = self.PGDOptim(
            lin_model=LinearModel(observations),    # [num observations, num predictors, dim sample]
            dataset_train=self.dataset_train, 
            dataset_val=self.dataset_val      
        )  # [num observations, num predictors, dim sample]
        print(f"{self.header} learned selectors size: {selectors.size()}\n")
        
        labels_eval, features_eval = self.dataset_eval[:]

        # perform model selection on evaluation set
        min_val, min_ids = torch.min(
            selectors.conditional_one_rate(
                X=features_eval,
                y=labels_eval.t()
            ),                      # [num observations, num predictors]
            dim=1
        )                           # [num observations], [num observations]

        # reduce the model to the best classifier-selector pair
        return min_val, min_ids, selectors.reduce(
            ids=min_ids,
            dim=1
        )
    
    def PGDOptim(
            self,
            lin_model: LinearModel,
            dataset_train: Union[Subset, Dataset],
            dataset_val: Subset = None
    ) -> LinearModel:
        """
        Perform the projected gradient descent optimization.

        Parameters:
        lin_model (LinearModel):                The sparse predictors.
        dataset_train (Union[Subset, Dataset]): The training dataset.
        dataset_val (Subset):                   The validation dataset, if necessary.

        Returns:
        selector (LinearModel):                 The selector model.
        """        

        # labels:   [num train sample, num predictors]
        # features: [num train sample, dim sample]
        labels_train, features_train = dataset_train[:]

        if dataset_val is not None:
            # labels:   [num val sample, num predictors]
            # features: [num val sample, dim sample]
            labels_val, features_val = dataset_val[:]

            min_weights, min_errors = self.error_tracker(
                weight_shape=lin_model.size(), 
                device=self.device
            )

            for i in range(self.num_iter):
                # update weights
                self.grad_update(
                    lin_model=lin_model,            # [num observations, num predictors, dim sample]
                    labels=labels_train,
                    features=features_train
                )

                # compute the conditional error rate
                conditional_error_rates = lin_model.conditional_one_rate(
                    X=features_val,             # [num val sample, dim sample]
                    y=labels_val.t()            # [num predictors, num val sample]
                )                               # [num observations, num predictors]

                # select the best selector between the current and the previous best
                min_weights, min_errors = self.pairwise_select(
                    curr_error=conditional_error_rates,
                    min_error=min_errors,       # [num observations, num predictors]
                    curr_weight=lin_model.weights,
                    min_weight=min_weights    # [num observations, num predictors, dim sample]
                )

            lin_model = LinearModel(min_weights)
        else:
            for i in range(self.num_iter):
                # update weights
                self.grad_update(
                    lin_model=lin_model,
                    labels=labels_train,
                    features=features_train
                )

        self.converged_bar.close()
        
        return lin_model
    
    def error_tracker(
            self,
            weight_shape: Union[List[int], torch.Size],
            device: torch.device
        ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Initialize the error tracker.

        Parameters:
        weight_shape (Union[List[int], torch.Size]):   The shape of the weights.
        device (torch.device):                         The device to be used.
        """

        # store the weights of the best linear models
        weights = torch.zeros(
            weight_shape
        ).to(device).squeeze() # [cluster_size, ..., dim_sample]

        # record the conditional error of the corresponding best linear models
        error = torch.ones(
            weight_shape[:-1]
        ).to(device).squeeze() # [cluster_size, ...]

        return weights, error
    
    def grad_update(
            self,
            lin_model: LinearModel,
            labels: torch.Tensor,       # labels:   [num train sample, num predictors]
            features: torch.Tensor      # features: [num train sample, dim sample]
    ) -> None:
        """
        Perform the gradient step for weights.
        
        Parameters:
        lin_model (LinearModel):         The linear model to be updated.
        labels (torch.Tensor):           The labels to be used.
        features (torch.Tensor):         The features to be used.
        """
        # compute projected gradients
        proj_grads = lin_model.proj_grad(
            X=features,
            y=labels.t()
        )

        # gradient step
        lin_model.update(
            weights= - self.lr * proj_grads
        )

        # update convergence progress
        self.converged_bar.n = int((torch.norm(proj_grads, p=2, dim=-1) < 0.025).sum())
        self.converged_bar.refresh()


    def pairwise_select(
            self,
            curr_error: torch.Tensor,
            min_error: torch.Tensor,
            curr_weight: torch.Tensor,
            min_weight: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """ 
        Update the weights based on the current error and the minimum error.

        Parameters:
        curr_error (torch.Tensor):      The current error.              [num observations, num predictors]
        min_error (torch.Tensor):       The minimum error.              [num observations, num predictors]
        curr_weight (torch.Tensor):     The current weight.             [num observations, num predictors, dim sample]
        min_weight (torch.Tensor):      The minimum weight.             [num observations, num predictors, dim sample]

        Returns:
        min_weight (torch.Tensor):      The updated minimum weight.     [num observations, num predictors, dim sample]
        min_error (torch.Tensor):       The updated minimum error.      [num observations, num predictors]
        """

        # print(f"{self.header}> updating - computing indices for weights that need to update ...")
        indices = curr_error < min_error   # [num observations, num predictors]
        # print(f"{self.header}> updating - updating errors ...")
        min_error = min_error * ~indices + curr_error * indices   # [num observations, num predictors]
        # print(f"{self.header}> updating - updating weights ...")
        min_weight = min_weight * ~indices.unsqueeze(-1) + curr_weight * indices.unsqueeze(-1) # [num observations, num predictors, dim sample]
        
        return min_weight, min_error