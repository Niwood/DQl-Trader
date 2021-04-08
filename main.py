from model import Agent
from environment import StockTradingEnv
from evaluation import ModelAssessment
from data_loader import DataLoader

from tqdm import tqdm
import numpy as np
import pandas as pd
import time
from statistics import mean 
from collections import deque
from datetime import datetime
from pathlib import Path
import json
import logging


import keras.backend as K
from sklearn.preprocessing import MinMaxScaler
from backtesting.test import GOOG
import pandas_ta as ta

from tools import safe_div


# Environment settings
EPISODES = 500
MAX_STEPS = 300

# Exploration settings
epsilon = 1 
epsilon_dsided_plateau = 10
MIN_EPSILON = 1e-6

#  Stats settings
AGGREGATE_STATS_EVERY = 1  #episodes
EPOCH_SIZE = 20

# For stats
ep_rewards = [0]


class Trader:

    def __init__(self):


        self.dataframe = 'sine'
        self.dl = DataLoader(dataframe=self.dataframe, remove_features=['close', 'high', 'low', 'open', 'volume'])
        self.df = self.dl.df

        self.num_time_steps = 300 #number of sequences that will be fed into the model
        self.agent = Agent(num_st_features=self.dl.num_st_features, num_lt_features=self.dl.num_lt_features, num_time_steps=self.num_time_steps)
        self.agent._pre_train(self.df, epochs=500, num_batches=1000)
        self.env = StockTradingEnv(
            self.df,
            look_back_window=self.num_time_steps,
            max_steps=MAX_STEPS,
            static_initial_step=0
            )
        # self.eval_env = StockTradingEnv(self.df, look_back_window=self.num_time_steps, max_steps=MAX_STEPS)

        # Epsilon
        self.epsilon_steps = [1]*epsilon_dsided_plateau + list(np.linspace(1,MIN_EPSILON,EPISODES - epsilon_dsided_plateau*2)) + [MIN_EPSILON]*(epsilon_dsided_plateau+1)


        # Statistics
        self.epsilon = epsilon
        stats = [
            'avgReward',
            'buyAndHold',
            'netWorthChng',
            'buyTrigger',
            'sellTrigger',
            'holdTrigger',
            'epsilon',
            'totTrainTime',
            'amountBalance',
            'amountAsset',
            'holdReward',
            'sellReward',
            'buyReward',
            'avgAmount'
            ]
        self.estats = pd.DataFrame(
            0,
            index=np.arange(1, EPISODES+1),
            columns=stats)
        self.estats.index.name = 'Episode'

        self.astats = pd.DataFrame(
            0,
            index=np.arange(EPOCH_SIZE, EPISODES+1, EPOCH_SIZE),
            columns=[
                'lastReward',
                'buyAndHold',
                'netWorthChng',
                'buyTrigger',
                'sellTrigger',
                'holdTrigger',
                'avgAmount',
                'networkName'
                ]
            )
        self.astats.index.name = 'Episode'
        
        # Create model folder and model ID
        self.model_id = int(time.time())
        self.agent.model._name = str(self.model_id)
        self.folder = Path.cwd() / 'models' / str(self.model_id)
        self.folder.mkdir(exist_ok=False)

        # Save architechture
        self.agent.model._name = self.model_id
        with open(self.folder / 'arch.txt','w') as fh:
            self.agent.model.summary(print_fn=lambda x: fh.write(x + '\n'))

        # Save metadata
        metadata = {
            'model_id': self.model_id,
            'date': str(datetime.now()),
            'episodes': EPISODES,
            'epoch_size': EPOCH_SIZE,
            'time_steps': self.num_time_steps,
            'data': {
                'df':self.dl.df_name,
                'length': len(self.df),
                'features': list(self.df.columns),
                'number_of_features': len(self.df.columns)
                },
            'optimizer': {
                'algorithm': K.eval(self.agent.model.optimizer._name),
                'learning_rate': float(K.eval(self.agent.model.optimizer.lr)),
                'decay': float(K.eval(self.agent.model.optimizer.decay))
                },
            # 'loss': K.eval(self.agent.model.loss.__name__),
            # 'metrics': K.eval(self.agent.model.metrics),
            'cost_matrix': str(self.agent.cost_matrix),
            'agent': {
                'discount': self.agent.DISCOUNT,
                'replay_memory_size': self.agent.REPLAY_MEMORY_SIZE,
                'min_replay_memory_size': self.agent.MIN_REPLAY_MEMORY_SIZE,
                'minibatch_size': self.agent.MINIBATCH_SIZE,
                'update_target_network_every': self.agent.UPDATE_TARGET_EVERY
                }
            }
        with open(self.folder / 'metadata.json', 'w') as outfile:
            json.dump(metadata, outfile)
            

        # Pre-train the network



        # Run
        self.run()
    


    def run(self):

        # Iterate over episodes
        for episode in tqdm(range(1, EPISODES + 1), ascii=True, unit='episodes'):

            # Slice estats for this episode for simplicity
            self._estats = self.estats.loc[episode]

            # Timer to track train time
            self.train_time = 0

            # Save actions
            self.actions = list()
            
            # Update tensorboard step every episode
            self.agent.tensorboard.step = episode

            # Reset episode reward and step number
            self.episode_reward = list()
            self.reward_action = [0, 0, 0]
            step = 1

            # Reset environment and get initial state
            current_state, _ = self.env.reset()

            # Reset flag and start iterating until episode ends
            done = False
            while not done:

                # This part stays mostly the same, the change is to query a model for Q values
                if np.random.random() > self.epsilon:
                    # Get action from Q table
                    action = np.argmax(self.agent.get_qs(current_state))
                else:
                    # Get random action
                    action = np.random.randint(0, self.env.ACTION_SPACE_SIZE)

                # STEP ENV
                new_state, reward, done = self.env.step(action)

                # Save reward and action
                self.reward_action[action] += reward
                self.actions.append(action)

                # Transform new continous state to new discrete state and count reward
                self.episode_reward.append(reward)

                # Every step we update replay memory and train main network
                self.agent.update_replay_memory((current_state, action, reward, new_state, done))
                self.agent.train(done, step)
                self.train_time += self.agent.elapsed
                
                current_state = new_state
                step += 1

            # Append episode reward to a list and log stats (every given number of episodes)
            ep_rewards.append(mean(self.episode_reward))

            # Save model
            if not episode % EPOCH_SIZE:
                self._save_model(episode)
            else:
                # Set default values to evaluation stats
                self._estats.loc['TotTrainTime'] = round(self.train_time,1)

            # Render
            if not episode % AGGREGATE_STATS_EVERY:
                self._render(episode)

            # Decay epsilon
            self.epsilon = self.epsilon_steps[episode]



    def _render(self, episode):
        ''' Renders stats for certain episodes '''
        print('='*20)

        # Env to render
        self.env.render(stats=self._estats)

        # Episode aggregated stats
        self._estats.loc['epsilon'] = round(self.epsilon,2)
        self._estats.loc['avgReward'] = round(mean(self.episode_reward),3)
        self._estats.loc['totTrainTime'] = round(self.train_time,1)
        self._estats.loc['holdTrigger'] = round( self.actions.count(0)/len(self.actions) ,3)
        self._estats.loc['buyTrigger'] = round( self.actions.count(1)/len(self.actions) ,3)
        self._estats.loc['sellTrigger'] = round( self.actions.count(2)/len(self.actions) ,3)
        self._estats.loc['holdReward'] = round( safe_div(self.reward_action[0], self.actions.count(0)) ,3)
        self._estats.loc['buyReward'] = round( safe_div(self.reward_action[1], self.actions.count(1)) ,3)
        self._estats.loc['sellReward'] = round( safe_div(self.reward_action[2], self.actions.count(2)) ,3)

        # Print episode stats
        self.estats.loc[episode] = self._estats
        print(self.estats.loc[episode-10:episode])

        # Pickle and save episode stats
        self.estats.loc[1:episode].to_pickle(self.folder / 'estats.pkl')


    def _save_model(self, episode):
        ''' For each epoch save model and make a sample inference with epsilon=0 '''
        epoch_id = int(time.time())
        self.last_model_name = f'{epoch_id}_EPS{episode}of{EPISODES}.model'
        self.agent.model.save(self.folder / self.last_model_name)
        self._model_assessment(episode)



    def _model_assessment(self, episode):
        ''' Make a model predict on a sample with epsilon=0 '''
        print('***** MODEL EVAL *****')

        # Load data
        df = DataLoader(dataframe=self.dataframe, remove_features=['close', 'high', 'low', 'open', 'volume']).df
        
        # Model assessment
        ma = ModelAssessment(
            dataframe=df,
            num_st_features=self.dl.num_st_features,
            num_lt_features=self.dl.num_lt_features,
            num_time_steps = self.num_time_steps
            )
        ma.astats = self.astats.loc[episode]
        ma.load_model(model_name=self.folder / self.last_model_name)
        ma.simulate()
        ma.render()
        self.astats.loc[episode] = ma.astats

        # Save stats for the last model name
        self.astats.loc[episode, 'networkName'] = self.last_model_name

        # Print assessment stats
        print(self.astats.loc[episode-10:episode])

        # Pickle and save assessment stats
        self.astats.loc[1:episode].to_pickle(self.folder / 'astats.pkl')


if __name__ == '__main__':
    import matplotlib.pyplot as plt
    import pandas as pd
    import cProfile
    import re
    import pstats


    profiler = cProfile.Profile()
    profiler.enable()
    Trader()
    profiler.disable()

    p = pstats.Stats(profiler)
    p.sort_stats('cumulative').print_stats(30)
