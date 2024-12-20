from src.init_setup import * # type: ignore
import os
#import psutil # type: ignore
import shutil
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3' 
import multiprocessing as mp
from multiprocessing import Process, Manager, Pool
from tqdm import tqdm # type: ignore
import numpy as np # type: ignore
import pandas as pd # type: ignore
import pynvml # type: ignore
import tensorflow as tf # type: ignore
from tensorflow.keras import layers, regularizers  # type: ignore
from tensorflow.keras.layers import Dense, LSTM, Dropout # type: ignore
from tensorflow.keras.models import Sequential, load_model # type: ignore
from tensorflow.keras.callbacks import History, EarlyStopping, ReduceLROnPlateau, ModelCheckpoint # type: ignore
from keras.utils import to_categorical
import tensorboard
import keras_tuner # type: ignore
from keras_tuner.tuners import RandomSearch, BayesianOptimization # type: ignore
from keras_tuner.tuners import RandomSearch # type: ignore
from keras.callbacks import History
import json
import csv
import pickle
import time
import re
import glob
import datetime
import tensorflow_decision_forests as tfdf
from sklearn.model_selection import KFold
from sklearn.metrics import mean_absolute_error


## This is for pool of workers if multiprocess; however there might be memory leak(?)

# def worker(args):
#     i, shared_pred_dict, model_fit_predict_func  = args
#     # Call the function that does the model fitting and prediction
#     # I assume `self.model_fit_predict` is the function you want to run in parallel
#     result = model_fit_predict_func(i)
#     shared_pred_dict[i] = result

## This is our custom loss function


# def custom_penalty(y_pred):
#     # High penalty for predictions with absolute value below 0.001
#     low_threshold_penalty = tf.where(tf.abs(y_pred) < 0.001, 1.0, 0.0)
    
#     # Linear penalty for predictions between 0.001 and 0.005
#     linear_penalty_region = tf.where((tf.abs(y_pred) >= 0.001) & (tf.abs(y_pred) <= 0.005), 1.0, 0.0)
#     linear_penalty = linear_penalty_region * (0.005 - tf.abs(y_pred))
    
#     return low_threshold_penalty + linear_penalty

#moze update do 2.14 i zrobic cummin
def custom_max_drawdown(y_pred):
    """ Calculate the maximum drawdown in y_pred using TensorArray. """
    # Create a TensorArray to store the cumulative minimum values
    cummin_array = tf.TensorArray(dtype=tf.float32, size=tf.size(y_pred))
    cummin_array = cummin_array.write(0, y_pred[0])  # Initialize with the first value

    # Compute cumulative minimum using TensorArray
    for i in tf.range(1, tf.size(y_pred)):
        min_val = tf.minimum(cummin_array.read(i-1), y_pred[i])
        cummin_array = cummin_array.write(i, min_val)

    # Convert TensorArray back to Tensor
    cummin = cummin_array.stack()

    # Calculate drawdown and maximum drawdown
    drawdown = y_pred - cummin
    max_drawdown = tf.reduce_max(drawdown)
    return max_drawdown

def pnl_loss(y_true, y_pred, lambda_penalty=2.0, gamma=1.0):
    # Loss due to difference in predicted vs. actual returns
    mse = tf.keras.losses.MSE(y_true, y_pred)
    sign_penalty = tf.where(tf.sign(y_true) == tf.sign(y_pred), 1.0, lambda_penalty)
    L_return = mse * sign_penalty
    
    # Loss due to drawdown
    L_drawdown = custom_max_drawdown(y_pred) * gamma
    
    return L_return + L_drawdown


def MADL_mod(y_true, y_pred):
    # Directional term
    directional_term = (-1) * tf.sign(y_true * y_pred) * tf.abs(y_true)
    
    # Magnitude term
    magnitude_term = 2*tf.square(y_pred - y_true)  
    
    # Combine and take the maximum with 0
    loss = tf.maximum(directional_term + magnitude_term, 0.0) + 4e-6
    
    # low_threshold_penalty = tf.where(tf.abs(y_pred) < 0.001, 2.0, 0.0)
    
    # # Linear penalty for predictions between 0.001 and 0.005
    # linear_penalty_region = tf.where((tf.abs(y_pred) >= 0.001) & (tf.abs(y_pred) <= 0.005), 1.4, 0.0)
    # linear_penalty = linear_penalty_region * (0.005 - tf.abs(y_pred))

    # Add the custom penalty
    # loss += low_threshold_penalty + linear_penalty
    
    # Return the mean loss over the batch
    return tf.reduce_mean(loss)

class RollingLSTM:
    def __init__(self) -> None:
        self.setup = Setup()
        self.config = self.setup.config
        self.timestamp = time.strftime("%Y-%m-%d_%H-%M")
        self.export_path = f'{self.setup.ROOT_PATH}{self.config["prep"]["ExportDir"]}{self.timestamp}/'
        if not os.path.isdir(self.export_path): os.mkdir(self.export_path)
        self.logger = logging.getLogger("Fit Predict") # type: ignore
        self.logger.addHandler(logging.StreamHandler()) # type: ignore
        print = self.logger.info
        self.tensorboard_logger = self.config["logger"]["TensorboardLoggerPath"]
        self.icsa_df_raw_path = self.setup.ROOT_PATH + self.config["raw"]["IcsaRawDF"]
        with open(self.config["prep"]["WindowSplitDict"], 'rb') as handle: self.window_dict = pickle.load(handle)
        self.logger.info(f"Loaded data dictionary!")
        #self.logger.info(f'GPU DETECTED: [{tf.test.is_gpu_available(cuda_only=True)}]')
        self.logger.info(f"Train window dimensions (features, targets): {self.window_dict['x_train'].shape}, "
                         f"{self.window_dict['y_train'].shape}")
        self.logger.info(f"Test window dimensions (features, targets): {self.window_dict['x_test'].shape}, "
                         f"{self.window_dict['y_test'].shape}")
        self.x_train, self.y_train, self.x_test, self.y_test = (
            self.window_dict['x_train'], self.window_dict['y_train'], self.window_dict['x_test'], self.window_dict['y_test']
        )
        self.logger.info('Successfully loaded split dictionary')
        # self.logger.info(f"GPU DETECTED: [{tf.config.list_physical_devices('GPU')}]")
        self.logger.info(f'TRAIN (features; targets): ({self.window_dict["x_train"].shape}; {self.window_dict["y_train"].shape})')
        self.logger.info(f'TEST: ({self.window_dict["x_test"].shape}; {self.window_dict["y_test"].shape})')

        
        self.x_train, self.y_train, self.x_test = (self.window_dict['x_train'], self.window_dict['y_train'], self.window_dict['x_test'])
        
        #for key in self.config["model"]: self.logger.info(f"{key}: {self.config['model'][key]}")
        self.early_stopping_min_delta = 0.0
        self.predictions = []
        self.timestamp = time.strftime("%Y-%m-%d_%H-%M")
        self.log_result = {}
        self.best_hp = keras_tuner.HyperParameters()

    def model_fit_predict(self, i, shared_pred_dict, shared_result_dict): # shared_pred_dict
        """
        Training example was implemented according to machine-learning-mastery forum
        The function takes data from the dictionary returned from splitWindows.create_windows function
        https://machinelearningmastery.com/stateful-stateless-lstm-time-series-forecasting-python/
        """
        # start_time = time.time()
        log_dir = self.tensorboard_logger # + time.strftime("%Y-%m-%d_%H-%M-%S")
        tensorboard_callback = tf.keras.callbacks.TensorBoard(log_dir=log_dir, histogram_freq=10, write_graph=True)

        # Hyperparameter tuning
        # self.logger.info("Building a model to tune")
        # tuner = RandomSearch(
        #     self.model_builder
        #     , objective="val_loss"
        #     , max_trials = int(self.config["model"]["HyperParamTuneTrials"])
        #     , executions_per_trial = 2
        #     , directory = "models", overwrite = True
        #     , project_name = f"model_window_{i}"
        # )
        # print('ble_')
        ## We use Bayesian for more efficient search

        tuner = BayesianOptimization(
            self.model_builder,
            objective="val_loss",
            max_trials=int(self.config["model"]["HyperParamTuneTrials"]),
            executions_per_trial=int(self.config["model"]["ModelsPerTrial"]),
            directory="models",
            overwrite=True,
            project_name=f"model_window_{i}",
            # Optional: You can set the number of initial points to randomly sample before starting the BO.
            # num_initial_points=10
        )

        # history = History()
        # print('ble_')
        validation_window_size = int(self.config["model"]["ValidationWindow"])
        if self.config["model"]["Problem"] == "classification":
            y_train = to_categorical(self.y_train[i][:-validation_window_size], num_classes=3)[:,0,:]
            y_val = to_categorical(self.y_train[i][-validation_window_size:], num_classes=3)[:,0,:]
        elif self.config["model"]["Problem"] == "regression":
            y_train = self.y_train[i][:-validation_window_size]
            y_val = self.y_train[i][-validation_window_size:]
        else:
            logging.error("Wrong problem type! Check config")
            raise
        # print('ble_')
        if i == 0:
            validation_set_shapes = (self.x_train[i][-validation_window_size:].shape, self.y_train[i][-validation_window_size:].shape)
            print(f"TRAIN (features, targets): ({self.x_train.shape}, {self.y_train.shape})\nVAL: ({validation_set_shapes[0]}, {validation_set_shapes[1]})")
        
        # # Validation & Early Stopping setup
        # es = EarlyStopping(monitor='val_loss', mode='min', verbose=0, patience=3, restore_best_weights=True, min_delta=self.early_stopping_min_delta)
        
        # # Hyperparameter tuning -> compare previous best model with current hyper-model
        # tuner_previous_best = RandomSearch(
        #     self.model_builder
        #     , hyperparameters = keras_tuner.HyperParameters(), tune_new_entries = False
        #     , objective = "val_loss", max_trials = 1
        #     , directory = self.config["prep"]["TunerHistoryDir"], overwrite = True, project_name = f"current_window_model_with_previous_best_params"
        # )
        # print(f"[{i}/{self.x_train.shape[0]}] Evaluating previous model -> previous best parameters")
        # tuner_previous_best.search(
        #     self.x_train[i][:-validation_window_size]
        #     , y_train
        #     , validation_data = (self.x_train[i][-validation_window_size:], y_val)
        #     , epochs = int(self.config["model"]["Epochs"])
        #     , batch_size = int(self.config["model"]["BatchSizeValidation"]) # it has to be validation one since there is no other way to specify, and obviously batch size <= sample size
        #     , shuffle = False, callbacks = [es, tensorboard_callback], verbose = 1
        # )
        # tuner_current = RandomSearch(
        #     self.model_builder
        #     , objective="val_loss"
        #     , max_trials = int(self.config["model"]["HyperParamTuneTrials"])
        #     , directory = self.config["prep"]["TunerHistoryDir"], overwrite = True, project_name = f"current_window_model"
        # )
        
        print(f"[{i}/{self.x_train.shape[0]}] Tuning the model")
        if i == 0: tuner.search_space_summary()
        
        # self.logger.info("[{i}/{self.x_train.shape[0]}] Tuning the model")
        validation_window_size = int(self.config["model"]["ValidationWindow"])
        if i==0:
            self.logger.info(f"Train window dimensions (features, targets): {self.x_train.shape}, " + f"{self.y_train.shape}")
            validation_set_shapes = (self.x_train[i][-validation_window_size:].shape, self.y_train[i][-validation_window_size:].shape)
            self.logger.info(f"Validation window dimensions (features, targets): {validation_set_shapes[0]}, " + f"{validation_set_shapes[1]}")
        #change patience for different loss function if needed
        mm = 1 if self.hp_lss=='MSE' else 1
        #early stopping and reducing lr on plateau
        es = EarlyStopping(monitor='val_loss', mode='min', verbose=1, patience=5*mm, restore_best_weights = True, min_delta=self.early_stopping_min_delta)
        reduce_lr = ReduceLROnPlateau(monitor='val_loss', factor=0.2, patience=5, min_lr=0.0001)
        
        # Benchmark - previous best model
        # Assume best_hp and best_weights_path are from the previous window
        report_dir = self.setup.ROOT_PATH + self.config["prep"]["ModelParamDir"]
        if i > 0:
            #prev_model = self.model_builder(None)
            #prev_model.load_weights(f'{report_dir}best_model_weights.h5')
            # Load the previously saved best model
            prev_model = load_model(f'{report_dir}best_model_window_{i-1}.h5')

        # Optionally, further fit the model on new data
            # Optionally, do a brief training session with the new window data
            hist = prev_model.fit(self.x_train[i][:-validation_window_size], y_train, epochs=int(self.config["model"]["Epochs"]), 
                           batch_size = int(self.config["model"]["BatchSizeValidation"]) 
                         , shuffle = False,
                      validation_data=(self.x_train[i][-validation_window_size:], y_val),
                      callbacks = [es, reduce_lr])


        # The search itself on new window
        tuner.search(
            self.x_train[i][:-validation_window_size]
            , y_train
            , validation_data = (self.x_train[i][-validation_window_size:], y_val)
            #, hyperparameters = keras_tuner.HyperParameters()
            , epochs = int(self.config["model"]["Epochs"])
            , batch_size = int(self.config["model"]["BatchSizeValidation"]) # it has to be validation one since there is no other way to specify, and obviously batch size <= sample size
            , shuffle = False, callbacks = [es, reduce_lr], verbose = 1
            # tensorboard_callback if needed
        )
        
        self.best_hp = tuner.get_best_hyperparameters(num_trials=1)[0]

        # Compare previous best model and current tuned model, retrieve best hyperparameters
        use_old_model = True
        if i>0:
            current_best_score = tuner.oracle.get_best_trials(1)[0].metrics.get_best_value('val_loss')
            previous_best_score = min(hist.history['val_loss'])
            print(previous_best_score)
            if previous_best_score > current_best_score:
                use_old_model = False
                self.best_hp = tuner.get_best_hyperparameters(1)[0]
                print(f"Current model outfperforms previous tuned model: Current score: {current_best_score}, Previous score: {previous_best_score} => ")
        # else: self.best_hp = tuner.get_best_hyperparameters(1)[0]
        
        # Save best combination
        # report_dir = self.setup.ROOT_PATH + self.config["prep"]["ModelParamDir"]
        with open(f'{report_dir}optimal_hyperparams_{self.timestamp}.csv', "a") as fp: 
            writer = csv.writer(fp, delimiter="\t",lineterminator="\n")
            if i == 0:
                writer.writerow(self.best_hp.values.keys())
            writer.writerow(self.best_hp.values.values())
        shutil.copy2(f'{report_dir}optimal_hyperparams_{self.timestamp}.csv', self.export_path)
        
        ## Check if old model is better than new
        if use_old_model and i>0:
            tuned_model = prev_model
        else:
            tuned_model = tuner.get_best_models(num_models=1)[0]

        # Save best model for future windows init
        tuned_model.save(f'{report_dir}best_model_window_{i}.h5')
        # tuned_model.save_weights(f'{report_dir}best_model_weights.h5')

        # # Build the tuned model and train it; use early stopping for epochs
        # tuned_model = tuner.hypermodel.build(optimal_hp)
        # history = tuned_model.fit(
        #     self.x_train[i][:-validation_window_size]
        #     , self.y_train[i][:-validation_window_size]
        #     , validation_data = (
        #         self.x_train[i][-validation_window_size:]
        #         , self.y_train[i][-validation_window_size:]
        #     )
        #     , epochs = int(self.config["model"]["Epochs"])
        #     , batch_size = int(self.config["model"]["BatchSizeTrain"])
        #     , shuffle = False
        #     , verbose = 1
        #     , callbacks = [es, tensorboard_callback]
        # )

        # Extracting Information from Trials
        for trial in tuner.oracle.trials.values():
            trial_id = trial.trial_id
            trial_info = {}
            
            # 1. Extract hyperparameters
            trial_info["hyperparameters"] = trial.hyperparameters.values
            
            # 2. Extract metrics
            trial_info["final_loss"] = trial.metrics.get_best_value('loss')
            trial_info["final_val_loss"] = trial.metrics.get_best_value('val_loss')
            
            # 3. Compute test_loss using the best model of this trial
            best_model = tuner.load_model(trial)  # tuner.get_best_models(num_models=1)[0]
            test_loss = best_model.evaluate(self.x_test[i], self.y_test[i], batch_size=int(self.config["model"]["BatchSizeTest"]), verbose=0)  # Make sure x_test and y_test are defined
            trial_info["test_loss"] = test_loss if isinstance(test_loss, float) else test_loss[0]

            # Store trial info in shared_result_dict using batch index and trial_id as key
            key = f"batch_{i}_trial_{trial_id}"
            shared_result_dict[key] = trial_info

        # generate array of predictions from ith window and save it to dictionary (shared between processes)
        predictions_array = tuned_model.predict(self.x_test[i], batch_size=int(self.config["model"]["BatchSizeTest"]), verbose=0)
        print(predictions_array)
        if self.config["model"]["Problem"] == "regression": shared_pred_dict[i] = predictions_array
        elif self.config["model"]["Problem"] == "classification": 
            classes = [0, 1, -1]
            shared_pred_dict[i] = [classes[most_probable_class] for most_probable_class in predictions_array.argmax(axis=-1)]
            print(predictions_array)
            print(predictions_array.argmax(axis=-1))
            print(shared_pred_dict[i])
        
        return 0
        # tuned_model.reset_states() not need since each process is independent

    def model_fit_predict_multiprocess(self, save=True) -> int:
        '''
        Executes model_fit_predict as separate processes. Processes share predictions dictionary.
        '''  
        mp.set_start_method('spawn', force=True)
        
        # Initialize fit history csv
        history_csv_path = f'{self.setup.ROOT_PATH}{self.config["prep"]["FitHistoryDir"]}fit_history_{self.timestamp}.csv'
        pd.DataFrame(columns=['loss', 'val_loss', 'window_index'])\
            .to_csv(history_csv_path, index=False)
        
        # Create separate process for each window. Save predictions to a dictionary shared between processes.
        with Manager() as manager:
            shared_pred_dict = manager.dict()
            shared_result_dict = manager.dict()
            processes = []
            print(f"Rolling Windows number: {self.x_train.shape[0]}")
            for i in range(self.x_train.shape[0]):
                p = Process(target=self.model_fit_predict, args=(i, shared_pred_dict, shared_result_dict))  # Passing the list
                processes.append(p)
                start_time = time.time()
                p.start()            
                p.join()
                self.logger.info(f'[{i}/{self.x_train.shape[0]}] FINISHED, TOTAL FINISHED: [{len(shared_pred_dict)}/{self.x_train.shape[0]}] [{i}]-th EXEC TIME: {time.time() - start_time}')
                # self.get_gpu_mem_usage(i)
            self.log_result = dict(shared_result_dict)
            self.predictions = [shared_pred_dict[key] for key in sorted(shared_pred_dict.keys())]

        ## For pool of workers, however seems unstable (memory leak?)
        # with Manager() as manager:
        #     shared_pred_dict = manager.dict()

        #     # Use half of the available logical cores to ensure system remains responsive
        #     num_processes = 1

        #     with Pool(processes=num_processes) as pool:
        #         pool.map(worker, [(i, shared_pred_dict, self.model_fit_predict) for i in range(self.x_train.shape[0])])

        #     # After processing, collect results
        #     self.predictions = [shared_pred_dict[key] for key in sorted(shared_pred_dict.keys())]


        if save == True:
            self.logger.info(f'Saving predictions')
            output_dir = self.setup.ROOT_PATH + self.config["prep"]["DataOutputDir"]
            if not os.path.isdir(output_dir): os.mkdir(output_dir)
            with open(self.config["prep"]["PredictionsArray"], 'wb') as handle: 
                pickle.dump(np.asarray(self.predictions, dtype=object), handle, protocol=pickle.HIGHEST_PROTOCOL)
            #shutil.copy2(history_csv_path, self.export_path)
            #shutil.copy2(f'{self.setup.ROOT_PATH}config.ini', self.export_path)

        return 0
    

    def model_builder(self, hp) -> Sequential:
        """
        A function building the sequential Keras model
        model_builder parameters are described in parameters.py script
        The model uses stacked LSTM layers with a dropout set in each of them
        Last LSTM layer before the output layer always has to have return_sequences=False
        """
        batch_input_shape = (int(self.config["model"]["BatchSizeValidation"])
                             , int(self.config["model"]["Lookback"])
                             , len(self.config["model"]["Features"].split(', ')))
        problem = self.config["model"]["Problem"]
        # If params from previous model not found, use default dictionary
        # probably not needed with current setup of loading
        previous_hp_csv = f'{self.config["prep"]["ModelParamDir"]}optimal_hyperparams_{self.timestamp}.csv'
        if os.path.isfile(previous_hp_csv):
            with open(previous_hp_csv, newline="\n") as fh:
                reader = csv.DictReader(fh, delimiter="\t")
                hp_default_dict = list(reader)[-1]
            
        else: hp_default_dict = {
            "learning_rate": self.config["model"]["DefaultLearningRate"],
            "loss_fun": self.config["model"]["DefaultLossFunction"] if problem == "regression" else self.config["model"]["DefaultLossFunctionClass"],
            "optimizer": self.config["model"]["DefaultOptimizer"],
            "units": self.config["model"]["DefaultUnits"],
            "hidden_layers": self.config["model"]["DefaultHiddenLayers"],
            "dropout": self.config["model"]["DefaultDropout"]
        }
        print(hp_default_dict)
        # Define hyperparameters grid
        hp_lr               = hp.Choice("learning_rate", values=[float(x) for x in self.config["model"]["LearningRate"].split(', ')])
        loss_fun_classification = {
            "categorical_crossentropy" : tf.keras.losses.CategoricalCrossentropy()
        }
        loss_fun_regression = {
            # "MAPE"  : tf.keras.losses.MeanAbsolutePercentageError(),
            "MSE" : tf.keras.losses.MeanSquaredError() ,
            "MADL2" : MADL_mod ,
            "PNL": pnl_loss
        }

        available_optimizers = {
            "Adam"        : tf.keras.optimizers.Adam(learning_rate=hp_lr)
            , "RMSprop"   : tf.keras.optimizers.RMSprop(learning_rate=hp_lr)
            , "Adadelta"  : tf.keras.optimizers.Adadelta(learning_rate=hp_lr)
        }

        if problem == "regression"      :   
            hp_loss_fun_name    = hp.Choice("loss_fun", default=hp_default_dict["loss_fun"], values=self.config["model"]["LossFunctionRegression"].split(', '))
            hp_loss_fun         = loss_fun_regression[hp_loss_fun_name]
            hp_activation       = self.config["model"]["ActivationFunctionRegression"]
            hp_dense_units      = 1
        elif problem == "classification":   
            hp_loss_fun_name    = hp.Choice("loss_fun", default=hp_default_dict["loss_fun"], values=self.config["model"]["LossFunctionClassification"].split(', '))
            hp_loss_fun         = loss_fun_classification[hp_loss_fun_name]
            hp_activation       = self.config["model"]["ActivationFunctionClassification"]
            hp_dense_units      = 3
        hp_optimizer            = available_optimizers[hp.Choice("optimizer", default=hp_default_dict["optimizer"], values=self.config["model"]["Optimizer"].split(', '))]
        hp_units                = hp.Int("units", default=int(hp_default_dict["units"]), min_value=int(self.config["model"]["LSTMUnitsMin"]), max_value=int(self.config["model"]["LSTMUnitsMax"]), step=16)
        hp_hidden_layers        = hp.Int("hidden_layers", default=int(hp_default_dict["hidden_layers"]), min_value=int(self.config["model"]["HiddenLayersMin"]), max_value=int(self.config["model"]["HiddenLayersMax"]), step=1)
        hp_dropout              = hp.Float("dropout", default=float(hp_default_dict["dropout"]), min_value=float(self.config["model"]["DropoutRateMin"]), max_value=float(self.config["model"]["DropoutRateMax"]), step=0.08)

        ## STARE BEZ DEFAULTOW DO WYWALENIA
        # if problem == "regression"      :   
        #     hp_loss_fun_name    = hp.Choice("loss_fun", default=hp_default_dict["loss_fun"], values=self.config["model"]["LossFunctionRegression"].split(', '))
        #     hp_loss_fun         = loss_fun_regression[hp_loss_fun_name]
        # elif problem == "classification":   
        #     hp_loss_fun_name    = hp.Choice("loss_fun", values=self.config["model"]["LossFunctionClassification"].split(', '))
        #     hp_loss_fun         = loss_fun_classification[hp_loss_fun_name]
        # hp_optimizer            = available_optimizers[hp.Choice("optimizer", self.config["model"]["Optimizer"].split(', '))]
        # hp_units                = hp.Int("units", min_value=int(self.config["model"]["LSTMUnitsMin"]), max_value=int(self.config["model"]["LSTMUnitsMax"]), step=16)
        # hp_hidden_layers        = hp.Int("hidden_layers", min_value=int(self.config["model"]["HiddenLayersMin"]), max_value=int(self.config["model"]["HiddenLayersMax"]), step=1)
        # hp_dropout              = hp.Float("dropout", min_value=float(self.config["model"]["DropoutRateMin"]), max_value=float(self.config["model"]["DropoutRateMax"]), step=0.08)
        self.early_stopping_min_delta = {
            "MSE": float(self.config["model"]["LossMinDeltaMSE"]), 
            "MAPE": float(self.config["model"]["LossMinDeltaMAPE"]),
            "MADL2": float(self.config["model"]["LossMinDeltaMADL"]),
            "PNL": float(self.config["model"]["LossMinDeltaPNL"]),
            "categorical_crossentropy": float(self.config["model"]["LossMinDeltaCategoricalCrossEntropy"])
            }[hp_loss_fun_name]
        hp_regularization = hp.Choice("regularization", values=[float(x) for x in self.config["model"]["Regularization"].split(', ')])
        self.hp_lss = hp_loss_fun_name
        # Sequential model definition:
        l1_ratio = 0.5  # Adjust this value. 1 = Lasso; 0 = Ridge
        l1_value = hp_regularization * l1_ratio
        l2_value = 0.01 * hp_regularization * (1 - l1_ratio)
        layer_list = []
        for _ in range(hp_hidden_layers-1): 
            layer_list.append(tf.keras.layers.LSTM(
                hp_units
                , batch_input_shape=batch_input_shape
                , activation=self.config["model"][f"ActivationFunction{problem}"]
                , stateful=True
                , dropout=hp_dropout
                , return_sequences=True
                , kernel_regularizer=regularizers.l1_l2(l1=l1_value, l2=l2_value),
                recurrent_regularizer=regularizers.l1_l2(l1=l1_value, l2=l2_value)
            ))
        layer_list.extend([
            layers.LSTM(hp_units, batch_input_shape=batch_input_shape
                , activation=self.config["model"][f"ActivationFunction{problem}"], dropout=hp_dropout
                , stateful=True, return_sequences=False
                , kernel_regularizer=regularizers.l1_l2(l1=l1_value, l2=l2_value),
                recurrent_regularizer=regularizers.l1_l2(l1=l1_value, l2=l2_value)  )
            , layers.Dense(1)
        ])
        model = tf.keras.Sequential(layer_list)
        model.compile(
            optimizer = hp_optimizer
            , loss = hp_loss_fun  
        )
        return model

    def save_results(self) -> int:
        """
        Save results and parameters to results directory
        """
        self.logger.info(f"Saving evaluation data and model description with timestamp: {self.timestamp}")
        closes = self.window_dict['closes_test']

        # Load predictions array
        with open(self.config["prep"]["PredictionsArray"], 'rb') as handle: preds = pickle.load(handle)
        try:

            df_pred_eval = pd.DataFrame(
                zip(self.window_dict['dates_test'].reshape(-1), preds.reshape(-1), self.window_dict['closes_test'].reshape(-1)), 
                columns=['Date', 'Pred', 'Real']
            )
        except:
            try:
                print('tutaj')
                df_pred_eval = pd.DataFrame(
                    zip(self.window_dict['dates_test'].reshape(-1), preds.reshape(-1), np.insert((closes[1:] - closes[:-1]) / closes[:-1], 0, np.nan).reshape(-1)),
                    columns=['Date', 'Pred', 'Real', "Real_ret"]
                )
            except:
                print('tutaj2')
                df_pred_eval = pd.DataFrame(
                    zip(self.window_dict['dates_test'].reshape(-1), preds.reshape(-1), self.window_dict['closes_test'].reshape(-1), ((closes[1:] - closes[:-1]) / closes[:-1]).reshape(-1)),
                    columns=['Date', 'Pred', 'Real', "Real_ret"]
                    
                )

        # Save summary of runs
        with open(f'{self.config["prep"]["ReportDirSummary"]}training_results_{self.timestamp}.json', "w") as file:
            json.dump(self.log_result, file, indent=4)
        print(df_pred_eval)
        # Save results to csv and pkl
        df_pred_eval.to_csv(f'{self.config["prep"]["DataOutputDir"]}model_eval_data_{self.timestamp}.csv', index=False)
        df_pred_eval.set_index("Date", inplace=True)
        df_pred_eval.to_pickle(f'{self.config["prep"]["DataOutputDir"]}model_eval_data_{self.timestamp}.pkl')
        
        return 0

    # def get_gpu_mem_usage(self, i) -> None:
        '''
        Checks GPU memory usage between window models
        '''
    #     pynvml.nvmlInit()
    #     h = pynvml.nvmlDeviceGetHandleByIndex(0)
    #     info = pynvml.nvmlDeviceGetMemoryInfo(h)
    #     self.logger.info(f"POST [{i}] GPU memory usage: {np.round(info.used/info.total*100, 2)}%")


    def convert_to_dataframe(X, y):
        # Assuming X has shape (num_samples, num_timesteps, num_features)
        num_samples = X.shape[0]
        num_timesteps = X.shape[1]
        num_features = X.shape[2]
        X_flat = X.reshape((num_samples, num_timesteps * num_features))
        feature_names = [f'feature_{i}' for i in range(X_flat.shape[1])]
        df_X = pd.DataFrame(X_flat, columns=feature_names)
        df_y = pd.DataFrame(y, columns=['target'])
        df = pd.concat([df_X, df_y], axis=1)
        return df


    def model_fit_predict_RF(self, i, shared_pred_dict, shared_result_dict): # shared_pred_dict
        """
        Training example was implemented according to machine-learning-mastery forum
        The function takes data from the dictionary returned from splitWindows.create_windows function
        https://machinelearningmastery.com/stateful-stateless-lstm-time-series-forecasting-python/
        """
        # start_time = time.time()
        log_dir = self.tensorboard_logger # + time.strftime("%Y-%m-%d_%H-%M-%S")
        tensorboard_callback = tf.keras.callbacks.TensorBoard(log_dir=log_dir, histogram_freq=10, write_graph=True)

        # tuner = BayesianOptimization(
        #     self.model_builder,
        #     objective="val_loss",
        #     max_trials=int(self.config["model"]["HyperParamTuneTrials"]),
        #     executions_per_trial=int(self.config["model"]["ModelsPerTrial"]),
        #     directory="models",
        #     overwrite=True,
        #     project_name=f"model_window_{i}",
        #     # Optional: You can set the number of initial points to randomly sample before starting the BO.
        #     # num_initial_points=10
        # )



        # history = History()
        # print('ble_')
        validation_window_size = int(self.config["model"]["ValidationWindow"])
        # if self.config["model"]["Problem"] == "classification":
        #     y_train = to_categorical(self.y_train[i][:-validation_window_size], num_classes=3)[:,0,:]
        #     y_val = to_categorical(self.y_train[i][-validation_window_size:], num_classes=3)[:,0,:]
        # elif self.config["model"]["Problem"] == "regression":
        #     y_train = self.y_train[i][:-validation_window_size]
        #     y_val = self.y_train[i][-validation_window_size:]
        # else:
        #     logging.error("Wrong problem type! Check config")
        #     raise
        # # print('ble_')
        # if i == 0:
        #     validation_set_shapes = (self.x_train[i][-validation_window_size:].shape, self.y_train[i][-validation_window_size:].shape)
        #     print(f"TRAIN (features, targets): ({self.x_train.shape}, {self.y_train.shape})\nVAL: ({validation_set_shapes[0]}, {validation_set_shapes[1]})")
        
                # Define the model
        rf_model = tfdf.keras.RandomForestModel(
            num_trees=100,
            max_depth=17,
            max_features=8,
            min_examples=2,  # Corresponds to min_samples_split in sklearn
            # Other hyperparameters can be set here
        )

        # Compile the model (no need to specify loss for regression)
        rf_model.compile(metrics=["mae"])

        # Fit the model
        rf_model.fit(train_df, label="target")

        # Evaluate on training data
        train_metrics = rf_model.evaluate(train_df, return_dict=True)

        # If you have a validation set
        val_df = convert_to_dataframe(self.x_train[i][-validation_window_size:], self.y_train[i][-validation_window_size:])
        val_metrics = rf_model.evaluate(val_df, return_dict=True)

        print(f"Training MAE: {train_metrics['mae']}")
        print(f"Validation MAE: {val_metrics['mae']}")

        # Instead of tuner.search(), use the Random Forest model

        # Prepare data
        train_df = convert_to_dataframe(self.x_train[i][:-validation_window_size], self.y_train[i][:-validation_window_size])
        val_df = convert_to_dataframe(self.x_train[i][-validation_window_size:], self.y_train[i][-validation_window_size:])
        if i == 0:
            validation_set_shapes = (train_df.shape, val_df.shape)
            print(f"TRAIN (features, targets): ({self.x_train.shape}, {self.y_train.shape})\nVAL: ({validation_set_shapes[0]}, {validation_set_shapes[1]})")

        # Define the model
        rf_model = tfdf.keras.RandomForestModel(
            num_trees=hp.Int("num_trees", min_value=50, max_value=200, step=50),
            max_depth=hp.Int("max_depth", min_value=5, max_value=20, step=5),
            max_features=hp.Choice("max_features", values=[None, "auto", "sqrt", "log2", 8]),
            min_examples=hp.Int("min_examples", min_value=2, max_value=10, step=2),
        )

        # Compile the model
        rf_model.compile(metrics=["mae"])

        # Fit the model
        rf_model.fit(train_df, label="target")

        # Evaluate the model
        val_metrics = rf_model.evaluate(val_df, return_dict=True)
        print(f"Validation MAE: {val_metrics['mae']}")


        # # Define the tuner builder function
        # def rf_model_builder(hp):
        #     rf_model = tfdf.keras.RandomForestModel(
        #         num_trees=hp.Int("num_trees", min_value=50, max_value=200, step=50),
        #         max_depth=hp.Int("max_depth", min_value=5, max_value=20, step=5),
        #         max_features=hp.Choice("max_features", values=[None, "auto", "sqrt", "log2", 8]),
        #         min_examples=hp.Int("min_examples", min_value=2, max_value=10, step=2),
        #         # Add other hyperparameters as needed
        #     )
        #     rf_model.compile(metrics=["mae"])
        #     return rf_model

        # from keras_tuner.tuners import BayesianOptimization

        # # Create the tuner
        # tuner = BayesianOptimization(
        #     rf_model_builder,
        #     objective="val_mae",
        #     max_trials=10,
        #     directory="rf_models",
        #     project_name=f"rf_model_window_{i}",
        # )

        # tuner.search(
        #     train_df,
        #     validation_data=val_df,
        #     label="target",
        # )

        # # Retrieve the best model
        # best_hp = tuner.get_best_hyperparameters(num_trials=1)[0]
        # best_model = tuner.get_best_models(num_models=1)[0]


        # Save the model
        rf_model.save(f'{report_dir}best_rf_model_window_{i}')

        # Load the model
        #loaded_model = tf.keras.models.load_model(f'{report_dir}best_rf_model_window_{i}')

        # # Evaluate the previous LSTM model on the validation set
        # prev_lstm_model = tf.keras.models.load_model(f'{report_dir}best_model_window_{i-1}.h5')
        # prev_val_metrics = prev_lstm_model.evaluate(
        #     self.x_train[i][-validation_window_size:], 
        #     self.y_train[i][-validation_window_size:], 
        #     batch_size=int(self.config["model"]["BatchSizeValidation"]), 
        #     return_dict=True
        # )

        # # Compare validation MAE
        # if val_metrics['mae'] < prev_val_metrics['mae']:
        #     print("Random Forest model outperforms the previous LSTM model.")
        #     # Proceed with the Random Forest model
        # else:
        #     print("Previous LSTM model performs better.")
        #     # Consider using the LSTM model or further tuning


        for i in range(num_windows):
            # Prepare data
            train_df = convert_to_dataframe(self.x_train[i][:-validation_window_size], self.y_train[i][:-validation_window_size])
            val_df = convert_to_dataframe(self.x_train[i][-validation_window_size:], self.y_train[i][-validation_window_size:])
            
            # Define and train the Random Forest model
            tuner = BayesianOptimization(
            rf_model_builder,
            objective="val_mae",
            max_trials=10,
            directory="rf_models",
            project_name=f"rf_model_window_{i}",
              )

            tuner.search(
            train_df,
            validation_data=val_df,
            label="target",
              )

            # Retrieve the best model
            best_hp = tuner.get_best_hyperparameters(num_trials=1)[0]
            best_model = tuner.get_best_models(num_models=1)[0]
            # (Include hyperparameter tuning if desired)
            
            # Save the best model
            rf_model.save(f'{report_dir}best_rf_model_window_{i}')
            
            # Optionally, compare with previous models and decide which to use

            for trial in tuner.oracle.trials.values():
                trial_id = trial.trial_id
                trial_info = {}
                
                # 1. Extract hyperparameters
                trial_info["hyperparameters"] = trial.hyperparameters.values
                
                # 2. Extract metrics
                trial_info["final_loss"] = trial.metrics.get_best_value('loss')
                trial_info["final_val_loss"] = trial.metrics.get_best_value('val_loss')
                
                # 3. Compute test_loss using the best model of this trial
                best_model = tuner.load_model(trial)  # tuner.get_best_models(num_models=1)[0]
                test_loss = best_model.evaluate(self.x_test[i], self.y_test[i], batch_size=int(self.config["model"]["BatchSizeTest"]), verbose=0)  # Make sure x_test and y_test are defined
                trial_info["test_loss"] = test_loss if isinstance(test_loss, float) else test_loss[0]

                # Store trial info in shared_result_dict using batch index and trial_id as key
                key = f"batch_{i}_trial_{trial_id}"
                shared_result_dict[key] = trial_info

            # generate array of predictions from ith window and save it to dictionary (shared between processes)
            predictions_array = best_model.predict(self.x_test[i], batch_size=int(self.config["model"]["BatchSizeTest"]), verbose=0)
            print(predictions_array)
            if self.config["model"]["Problem"] == "regression": shared_pred_dict[i] = predictions_array
            elif self.config["model"]["Problem"] == "classification": 
                classes = [0, 1, -1]
                shared_pred_dict[i] = [classes[most_probable_class] for most_probable_class in predictions_array.argmax(axis=-1)]
                print(predictions_array)
                print(predictions_array.argmax(axis=-1))
                print(shared_pred_dict[i])
        










        # print(f"[{i}/{self.x_train.shape[0]}] Tuning the model")
        # if i == 0: tuner.search_space_summary()
        
        # # self.logger.info("[{i}/{self.x_train.shape[0]}] Tuning the model")
        # validation_window_size = int(self.config["model"]["ValidationWindow"])
        # if i==0:
        #     self.logger.info(f"Train window dimensions (features, targets): {self.x_train.shape}, " + f"{self.y_train.shape}")
        #     validation_set_shapes = (self.x_train[i][-validation_window_size:].shape, self.y_train[i][-validation_window_size:].shape)
        #     self.logger.info(f"Validation window dimensions (features, targets): {validation_set_shapes[0]}, " + f"{validation_set_shapes[1]}")
        # #change patience for different loss function if needed
        # mm = 1 if self.hp_lss=='MSE' else 1
        # #early stopping and reducing lr on plateau
        # es = EarlyStopping(monitor='val_loss', mode='min', verbose=1, patience=5*mm, restore_best_weights = True, min_delta=self.early_stopping_min_delta)
        # reduce_lr = ReduceLROnPlateau(monitor='val_loss', factor=0.2, patience=5, min_lr=0.0001)
        
        # Benchmark - previous best model
        # Assume best_hp and best_weights_path are from the previous window
        # report_dir = self.setup.ROOT_PATH + self.config["prep"]["ModelParamDir"]
        # if i > 0:
        #     #prev_model = self.model_builder(None)
        #     #prev_model.load_weights(f'{report_dir}best_model_weights.h5')
        #     # Load the previously saved best model
        #     prev_model = load_model(f'{report_dir}best_model_window_{i-1}.h5')

        # # Optionally, further fit the model on new data
        #     # Optionally, do a brief training session with the new window data
        #     hist = prev_model.fit(self.x_train[i][:-validation_window_size], y_train, epochs=int(self.config["model"]["Epochs"]), 
        #                    batch_size = int(self.config["model"]["BatchSizeValidation"]) 
        #                  , shuffle = False,
        #               validation_data=(self.x_train[i][-validation_window_size:], y_val),
        #               callbacks = [es, reduce_lr])


        # # The search itself on new window
        # tuner.search(
        #     self.x_train[i][:-validation_window_size]
        #     , y_train
        #     , validation_data = (self.x_train[i][-validation_window_size:], y_val)
        #     #, hyperparameters = keras_tuner.HyperParameters()
        #     , epochs = int(self.config["model"]["Epochs"])
        #     , batch_size = int(self.config["model"]["BatchSizeValidation"]) # it has to be validation one since there is no other way to specify, and obviously batch size <= sample size
        #     , shuffle = False, callbacks = [es, reduce_lr], verbose = 1
        #     # tensorboard_callback if needed
        # )
        
        # self.best_hp = tuner.get_best_hyperparameters(num_trials=1)[0]

        # # Compare previous best model and current tuned model, retrieve best hyperparameters
        # use_old_model = True
        # if i>0:
        #     current_best_score = tuner.oracle.get_best_trials(1)[0].metrics.get_best_value('val_loss')
        #     previous_best_score = min(hist.history['val_loss'])
        #     print(previous_best_score)
        #     if previous_best_score > current_best_score:
        #         use_old_model = False
        #         self.best_hp = tuner.get_best_hyperparameters(1)[0]
        #         print(f"Current model outfperforms previous tuned model: Current score: {current_best_score}, Previous score: {previous_best_score} => ")
        # # else: self.best_hp = tuner.get_best_hyperparameters(1)[0]
        
        # # Save best combination
        # # report_dir = self.setup.ROOT_PATH + self.config["prep"]["ModelParamDir"]
        # with open(f'{report_dir}optimal_hyperparams_{self.timestamp}.csv', "a") as fp: 
        #     writer = csv.writer(fp, delimiter="\t",lineterminator="\n")
        #     if i == 0:
        #         writer.writerow(self.best_hp.values.keys())
        #     writer.writerow(self.best_hp.values.values())
        # shutil.copy2(f'{report_dir}optimal_hyperparams_{self.timestamp}.csv', self.export_path)
        
        # ## Check if old model is better than new
        # if use_old_model and i>0:
        #     tuned_model = prev_model
        # else:
        #     tuned_model = tuner.get_best_models(num_models=1)[0]

        # Save best model for future windows init
        # tuned_model.save(f'{report_dir}best_model_window_{i}.h5')
        # tuned_model.save_weights(f'{report_dir}best_model_weights.h5')

        # # Build the tuned model and train it; use early stopping for epochs
        # tuned_model = tuner.hypermodel.build(optimal_hp)
        # history = tuned_model.fit(
        #     self.x_train[i][:-validation_window_size]
        #     , self.y_train[i][:-validation_window_size]
        #     , validation_data = (
        #         self.x_train[i][-validation_window_size:]
        #         , self.y_train[i][-validation_window_size:]
        #     )
        #     , epochs = int(self.config["model"]["Epochs"])
        #     , batch_size = int(self.config["model"]["BatchSizeTrain"])
        #     , shuffle = False
        #     , verbose = 1
        #     , callbacks = [es, tensorboard_callback]
        # )

        # # Extracting Information from Trials
        # for trial in tuner.oracle.trials.values():
        #     trial_id = trial.trial_id
        #     trial_info = {}
            
        #     # 1. Extract hyperparameters
        #     trial_info["hyperparameters"] = trial.hyperparameters.values
            
        #     # 2. Extract metrics
        #     trial_info["final_loss"] = trial.metrics.get_best_value('loss')
        #     trial_info["final_val_loss"] = trial.metrics.get_best_value('val_loss')
            
        #     # 3. Compute test_loss using the best model of this trial
        #     best_model = tuner.load_model(trial)  # tuner.get_best_models(num_models=1)[0]
        #     test_loss = best_model.evaluate(self.x_test[i], self.y_test[i], batch_size=int(self.config["model"]["BatchSizeTest"]), verbose=0)  # Make sure x_test and y_test are defined
        #     trial_info["test_loss"] = test_loss if isinstance(test_loss, float) else test_loss[0]

        #     # Store trial info in shared_result_dict using batch index and trial_id as key
        #     key = f"batch_{i}_trial_{trial_id}"
        #     shared_result_dict[key] = trial_info

        # # generate array of predictions from ith window and save it to dictionary (shared between processes)
        # predictions_array = tuned_model.predict(self.x_test[i], batch_size=int(self.config["model"]["BatchSizeTest"]), verbose=0)
        # print(predictions_array)
        # if self.config["model"]["Problem"] == "regression": shared_pred_dict[i] = predictions_array
        # elif self.config["model"]["Problem"] == "classification": 
        #     classes = [0, 1, -1]
        #     shared_pred_dict[i] = [classes[most_probable_class] for most_probable_class in predictions_array.argmax(axis=-1)]
        #     print(predictions_array)
        #     print(predictions_array.argmax(axis=-1))
        #     print(shared_pred_dict[i])
        
        # return 0
        # tuned_model.reset_states() not need since each process is independent

    def shapp():
        import shap

        # Prepare data for SHAP
        X_train_flat = self.x_train[i][:-validation_window_size].reshape((num_samples, -1))
        explainer = shap.TreeExplainer(rf_model)
        shap_values = explainer.shap_values(X_train_flat)

        # Plot SHAP values
        shap.summary_plot(shap_values, X_train_flat)

### Extensions
# from sklearn.ensemble import RandomForestRegressor
# from sklearn.metrics import mean_squared_error

# # Assuming X_train, y_train, X_test, and y_test are your train-test data

# # Train the model
# rf = RandomForestRegressor(n_estimators=100)
# rf.fit(X_train, y_train)

# # Predict and evaluate
# y_pred = rf.predict(X_test)
# print(mean_squared_error(y_test, y_pred))

# # Feature importance
# importances = rf.feature_importances_

# import xgboost as xgb

# # Convert data to DMatrix format for efficiency
# dtrain = xgb.DMatrix(X_train, label=y_train)
# dtest = xgb.DMatrix(X_test, label=y_test)

# # Set parameters
# param = {
#     'max_depth': 3,  # Maximum depth of a tree
#     'eta': 0.3,      # Learning rate
#     'objective': 'reg:squarederror'
# }
# num_round = 100  # Number of boosting rounds

# # Train the model
# bst = xgb.train(param, dtrain, num_round)

# # Predict and evaluate
# y_pred = bst.predict(dtest)
# print(mean_squared_error(y_test, y_pred))

# # Feature importance
# importances = bst.get_fscore()

# import matplotlib.pyplot as plt

# # For Random Forest:
# sorted_idx = importances.argsort()
# plt.barh(range(X_train.shape[1]), importances[sorted_idx])
# plt.yticks(range(X_train.shape[1]), X_train.columns[sorted_idx])
# plt.xlabel('Importance')
# plt.show()

# # For XGBoost:
# sorted_idx = importances.argsort()
# plt.barh(range(X_train.shape[1]), bst.get_fscore()[sorted_idx])
# plt.yticks(range(X_train.shape[1]), X_train.columns[sorted_idx])
# plt.xlabel('Importance')
# plt.show()

# class AttentionTime(Layer):
#     def __init__(self, return_sequences=True, **kwargs):
#         self.return_sequences = return_sequences
#         super(AttentionTime, self).__init__(**kwargs)

#     def build(self, input_shape):
#         self.W = self.add_weight(shape=(input_shape[2], input_shape[2]),
#                                  initializer='uniform', trainable=True)
#         self.b = self.add_weight(shape=(input_shape[1], 1),
#                                  initializer='uniform', trainable=True)
#         super(AttentionTime, self).build(input_shape)

#     def call(self, x):
#         e = K.tanh(K.dot(x, self.W) + self.b)
#         a = K.softmax(e, axis=1)
#         output = x * a

#         if self.return_sequences:
#             return output
#         return K.sum(output, axis=1)

# class AttentionFeatures(Layer):
#     def __init__(self, **kwargs):
#         super(AttentionFeatures, self).__init__(**kwargs)

#     def build(self, input_shape):
#         self.W = self.add_weight(shape=(input_shape[2], input_shape[2]),
#                                  initializer='uniform', trainable=True)
#         self.b = self.add_weight(shape=(input_shape[2], 1),
#                                  initializer='uniform', trainable=True)
#         super(AttentionFeatures, self).build(input_shape)

#     def call(self, x):
#         e = K.tanh(K.dot(x, self.W) + self.b)
#         a = K.softmax(e, axis=2)
#         output = x * a
#         return K.sum(output, axis=2)

