"""
College Football Data Demo: Toolbox
Author: Trevor Cross
Last Updated: 06/30/22

Series of functions used to extract and analyze data from collegefootballdata.com.
"""

# ----------------------
# ---Import Libraries---
# ----------------------

# import standard libraries
import numpy as np
import pandas as pd

# import performance library
from numba import jit

# import support libraries
import requests as req
import json
from os.path import join
from tqdm import tqdm
from operator import itemgetter
from datetime import datetime
from scipy.stats import lognorm

# import visualization libraries
import matplotlib.pyplot as plt
from sklearn.metrics import confusion_matrix, ConfusionMatrixDisplay

# import snowflake connector
import snowflake.connector
from snowflake.connector.pandas_tools import write_pandas

# ---------------------------------
# ---Define SF and API Functions---
# ---------------------------------

# define CFD function
## define a function to make requests
def make_request(url, api_key):
    
    # define headers
    headers = {"Content-Type": "application/json",
               "Authorization": "Bearer {}".format(api_key)}

    # return API call as df
    return pd.json_normalize(req.get(url, headers=headers).json())

## define a function to build the URL
def build_url(base_url, section, sub_section='', filters=''):
    
    # combine base_url and section_name
    final_url = join(base_url, section)
    
    # combine w/ sub_section if exists
    if sub_section != '':
        final_url = join(final_url, sub_section)
        
    # combine w/ filters if exists
    if filters != '':
        final_url = final_url + filters
        
    # return final URL
    return final_url

## define a function to build filters
def build_filter(filter_names, filter_plugins):
    final_filter = "?"
    
    # build filter
    for filter_num, filter_name in enumerate(filter_names):
        final_filter =  final_filter + filter_name + "=" + str(filter_plugins[filter_num]) + "&"
    
    # return final filter (remove last '&')
    return final_filter[:-1]

## define a function to get column name-dtype string pairs & arrange them for SF
def get_col_info(df):
    
    # define dictionary to change Python dtype to SF dtype
    dtype_dict = {"bool":"boolean",
                  "object":"string",
                  "int64":"integer",
                  "float64":"float"
                  }
    
    # get column info pairs
    col_info = df.dtypes.reset_index().astype(str).replace(dtype_dict).apply(tuple, axis=1).tolist()
    
    # return each pair as a single string separated by a comma
    return (','.join([tup[0] + " " + tup[1] for tup in col_info])).replace('.','_')

# define SF functions
## define a function to connect to SF
def connect_to_SF(json_creds_path):
    
    # read JSON & connect to SF
    with open(json_creds_path) as file:
        creds = json.load(file)
        
    conn = snowflake.connector.connect(user=creds['user'],
                                           password=creds['password'],
                                           account=creds['account'],
                                           warehouse=creds['warehouse'],
                                           database=creds['database'],
                                           schema=creds['schema'])
    
    # return connector
    return conn

## define function to create table in SF
def create_table(conn, table_name, col_info):
    
    # create table
    conn.cursor().execute(
        """
        CREATE TABLE
        {}({})
        """.format(table_name, col_info)
        )
    
    # print result
    print("\n >>> Table {} created!".format(table_name.upper()))
    
## define a function to append data into table in SF
def append_data(conn, df, table_name):
    
    # capitalize columns
    df.columns = map(lambda name: name.upper().replace('.','_'), df.columns)
    
    # write to table
    success, num_chunks, num_rows, _ = write_pandas(conn, df, table_name.upper())
    
    # print result
    if success:
        print("\n >>> {} rows appended to table {}!".format(num_rows, table_name.upper()))
    else:
        print("\n >>> Something went wrong...")

# -------------------------------------------
# ---Define Elo Rating Algorithm Functions---
# -------------------------------------------

## define function to get initial Elo rating
def get_init_rat(team_name, fbs_team_list):
    
    # if fbs team, return higher init rating
    if team_name in fbs_team_list:
        return 1500
    
    # if not fbs team, return lower init rating
    else:
        return 1200

## define function to calculate margin of victory bonus
jit(nopython=True)
def MOV_mult(home_rat, away_rat, margin):
    return np.log(abs(margin)+1) * ( 2.2 / (abs(home_rat - away_rat)*10**-3 + 2.2) )

## define function to calculate Elo confidence
jit(nopython=True)
def calc_conf(rat_a, rat_b, scaler=400):
    return 1 / ( 1 + pow(10, (rat_b-rat_a)/scaler) )

## define function to calculate new Elo rating
jit(nopython=True)
def calc_new_rats(home_rat, away_rat, margin, K=25, scaler=400):
    
    # calc home & away confidence
    home_conf = calc_conf(home_rat, away_rat, scaler=scaler)
    away_conf = 1 - home_conf
    
    # determine actualized home confidence
    if margin > 0:
        home_act = 1
    elif margin < 0:
        home_act = 0
    else:
        home_act = 0.5
    
    # calc actualized away confidence
    away_act = 1 - home_act
    
    # calc margin of victory multiplier
    mult = MOV_mult(home_rat, away_rat, margin)
    
    # calc new home & away ratings
    home_rat_new = home_rat + mult*K*(home_act - home_conf)
    away_rat_new = away_rat + mult*K*(away_act - away_conf)
    
    # return new ratings, confidence, and actualized value
    return (round(home_rat_new), home_conf, home_act), (round(away_rat_new), away_conf, away_act)


## define function to run elo simulation
def run_elo_sim(game_df, fbs_team_list, 
                retain_weight=0.90, K=25, scaler=400):

    # create dictionary to record team Elo ratings
    team_rats = dict()
    
    # iterate through games
    for game_num, game in tqdm(game_df.iterrows(), desc='Running Elo Sim ', unit='game', total=game_df.shape[0]):
        
        # parse current date
        date = str(datetime.strptime(game['START_DATE'][0:10], '%Y-%m-%d').date())
    
        # if home team exists and in same season
        if game['HOME_TEAM'] in team_rats and int(team_rats[game['HOME_TEAM']][-1][0][:4]) == int(date[:4]):
            
            # get current home rating
            home_rat = team_rats[game['HOME_TEAM']][-1][1]
        
        # if home team exists and NOT in same season
        elif game['HOME_TEAM'] in team_rats:
            
            # get initial rating
            init_rat = get_init_rat(game['HOME_TEAM'], fbs_team_list)
            
            # reset home rating
            home_rat = int(retain_weight*(team_rats[game['HOME_TEAM']][-1][1]-init_rat) + init_rat)
        
        # if NOT home team exists
        else:
            
            # get initial rating
            init_rat = get_init_rat(game['HOME_TEAM'], fbs_team_list)
            
            # append home team to dict
            team_rats[game['HOME_TEAM']] = [(date, init_rat, None, None)]
            home_rat = team_rats[game['HOME_TEAM']][-1][1]
        
        # if away team exists and in same season
        if game['AWAY_TEAM'] in team_rats and int(team_rats[game['AWAY_TEAM']][-1][0][:4]) == int(date[:4]):
            
            # get current home rating
            away_rat = team_rats[game['AWAY_TEAM']][-1][1]
        
        # if away team exists and NOT in same season
        elif game['AWAY_TEAM'] in team_rats:
            
            # get initial rating
            init_rat = get_init_rat(game['AWAY_TEAM'], fbs_team_list)
            
            # reset away rating
            away_rat = int(retain_weight*(team_rats[game['AWAY_TEAM']][-1][1]-init_rat) + init_rat)
        
        # if NOT away team exists
        else:
            
            # get initial rating
            init_rat = get_init_rat(game['AWAY_TEAM'], fbs_team_list)
            
            # append away team to dict
            team_rats[game['AWAY_TEAM']] = [(date, init_rat, None, None)]
            away_rat = team_rats[game['AWAY_TEAM']][-1][1]
        
        # calc score margin from game
        margin = game['HOME_POINTS'] - game['AWAY_POINTS']
    
        # calc new ratings
        home_info, away_info = calc_new_rats(home_rat, away_rat, margin, K=K, scaler=scaler)
        home_rat_new, home_conf, home_act = home_info
        away_rat_new, away_conf, away_act = away_info
        
        # append new ratings to dict
        team_rats[game['HOME_TEAM']].append( (date, home_rat_new, home_conf, home_act) )
        team_rats[game['AWAY_TEAM']].append( (date, away_rat_new, away_conf, away_act) )
        
    # return dictionary of team Elo ratings
    return team_rats
    
## define function to plot ratings
def plot_rats(team_rats, team_name):
    
        # extract dates and ratings
        dates_list = list(map(itemgetter(0), team_rats[team_name]))
        rats_list = list(map(itemgetter(1), team_rats[team_name]))
        
        # define graph styling
        plt.style.use('bmh')
        plt.rcParams["figure.figsize"] = [20,10]
        
        # plot ratings against date
        plt.plot(dates_list, rats_list)
        
        # add title
        plt.title(team_name + ' Elo Ratings')
        
        # adjust xticks
        seasons = []
        season_start = []
        for date in dates_list:
            if date[:4] not in seasons:
                seasons.append(date[:4])
                season_start.append(date)
                
        plt.xticks(season_start, rotation=45)

# ----------------------------------------
# ---Define Record Prediction Functions---
# ----------------------------------------

## define a function to fit MOV data
def fit_MOV_data(game_df):
    
    # get MOV data
    MOV_data = abs(game_df['HOME_POINTS'] - game_df['AWAY_POINTS'])
    
    # get lognormal parameters
    s, loc, scale = lognorm.fit(MOV_data)
    
    # generate lognormal distribution of scores
    x = np.arange(0, max(MOV_data), 1)
    
    ## for some (dumb) reason, lognorm.pdf does not produce normalized vaues
    y = lognorm.pdf(x, s, loc, scale)
    y /= np.sum(y)
    
    # return x and y values
    return (x,y)

## define function to sample game results
jit(nopython=True)
def sample_game_results(home_rat, away_rat, margin_dist, K=25, scaler=400):
    
    # calc confidence
    home_conf = calc_conf(home_rat, away_rat, scaler)
    away_conf = 1 - home_conf
    
    # generate random number
    rand_num = np.random.uniform(low=0, high=1)
    
    # determine victor
    if home_conf > rand_num:
        home_act = 1
    else:
        home_act = 0
    
    # calc away actualized value
    away_act = 1 - home_act
    
    # sample margin from distribution
    margin = round_up(np.random.choice(margin_dist[0], p=margin_dist[1]))
    
    # calc MOV multiplier
    mult = MOV_mult(home_rat, away_rat, margin)
    
    # calc new home & away ratings
    home_rat_new = home_rat + mult*K*(home_act - home_conf)
    away_rat_new = away_rat + mult*K*(away_act - away_conf)
    
    # return new ratings, confidence, and actualized value
    return (round(home_rat_new), home_conf, home_act), (round(away_rat_new), away_conf, away_act)
    

## define a function to run record prediction for a season
def run_season_sim(season, game_df, fbs_team_list, team_rats, K=25, scaler=400):
    
    # create dictionary to record hot team ratings
    list_of_sims = dict()
    
    # filter game_df by season
    game_df_hot = game_df.loc[game_df['START_DATE'].str.startswith(str(season))]
    
    # calc prob distribution of MOV
    game_df_cold = game_df.loc[game_df['START_DATE'].str[:4].astype(int) < season]
    margin_dist = fit_MOV_data(game_df_cold)
    
    # iterate games
    for game_num, game in game_df_hot.iterrows():
        
        # parse current date
        date = str(datetime.strptime(game['START_DATE'][0:10], '%Y-%m-%d').date())
        
        # if home team exists
        if game['HOME_TEAM'] in list_of_sims:
            
            # get current home rating
            home_rat = list_of_sims[game['HOME_TEAM']][-1][1]
        
        # if NOT home team exists
        else:
            
            # get starting rating for the season
            home_rat = next(team_rats[game['HOME_TEAM']][items_num-1][1] for items_num, items in enumerate(team_rats[game['HOME_TEAM']]) if items[0][:4]==str(season))
            
            # append home team to dict
            list_of_sims[game['HOME_TEAM']] = []
            
        # if home team exists
        if game['AWAY_TEAM'] in list_of_sims:
            
            # get current home rating
            away_rat = list_of_sims[game['AWAY_TEAM']][-1][1]
        
        # if NOT away team exists
        else:
            
            # get starting rating for the season
            away_rat = next(team_rats[game['AWAY_TEAM']][items_num-1][1] for items_num, items in enumerate(team_rats[game['AWAY_TEAM']]) if items[0][:4]==str(season))
            
            # append away team to dict
            list_of_sims[game['AWAY_TEAM']] = []
            
        # sample game results
        home_info, away_info = sample_game_results(home_rat, away_rat, margin_dist, K=K, scaler=scaler)
        home_rat_new, home_conf, home_act = home_info
        away_rat_new, away_conf, away_act = away_info
        
        # append new ratings to dict
        list_of_sims[game['HOME_TEAM']].append( (date, home_rat_new, home_conf, home_act) )
        list_of_sims[game['AWAY_TEAM']].append( (date, away_rat_new, away_conf, away_act) )
        
    # return dictionary of team Elo ratings
    return list_of_sims

# ----------------------------
# ---Define Other Functions---
# ----------------------------

## define a function to round numbers up
jit(nopython=True)
def round_up(x):
    return int(x) + (x % 1 > 0)

## define a function to calculate log base n
jit(nopython=True)
def log_n(x, n=10):
    return np.log(x) / np.log(n)

## define a function to plot confusion matrix
def disp_conf_mat(preds, acts):
    conf_mat = confusion_matrix(acts, preds)
    ConfusionMatrixDisplay(conf_mat).plot()
    
## define a function to take the cartesian product of an arbitrary number of lists
jit(nopython=True)
def cart_prod(list_of_lists):
    
    # check argument is list of lists
    for lth in list_of_lists:
        if not isinstance(lth, list):
            raise TypeError("\n >>> The argument should be a list containing only lists.")
    
    # define recursive function
    def inner_cart_prod(list_0, list_1):
        inner_result = []
        for lth in list_0:
            for mth in list_1:
                inner_result.append([lth,mth])
        return inner_result
    
    # define funciton to flatten 2d list
    def flatten(nested_list):
        flat_list = []
        for el in nested_list:
            if isinstance(el, list):
                flat_list.extend(el)
            else:
                flat_list.append(el)
        return flat_list
    
    # exhaust recursion
    if len(list_of_lists) > 1:
        result = inner_cart_prod(list_of_lists[0], list_of_lists[1])
        for nth_pos in range(len(list_of_lists)-2):
            result = list(map(flatten, inner_cart_prod(result, list_of_lists[nth_pos+2])))

        return [tuple(lth) for lth in result]
    
    else:
        return [tuple([lth]) for lth in list_of_lists[0]]

## define function to save dictionary as JSON file locally
def dict_to_json(my_dict, file_path):
    with open(file_path, "w+") as file:
        json.dump(my_dict, file)

## define function to load local JSON file as Python dict
def json_to_dict(file_path):
    with open(file_path) as file:
        return json.load(file)