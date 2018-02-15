"""
Copyright 2017 The Johns Hopkins University Applied Physics Laboratory LLC
All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

__author__ = 'jhuapl'
__version__ = 0.1
import sys
sys.path.insert(0, "./data_ml_functions/DenseNet/")
#sys.path.insert(0, "/wdata/code/data_ml_functions/DenseNet/")
import json
#import tensorflow as tf
#from keras.backend.tensorflow_backend import set_session

#config = tf.ConfigProto()
#config.gpu_options.per_process_gpu_memory_fraction = 0.7
#set_session(tf.Session(config=config))
from keras.optimizers import Adam
from keras.optimizers import SGD
from keras.callbacks import ModelCheckpoint
from keras.preprocessing import image
from keras.models import Model, load_model
from keras.applications import imagenet_utils
from data_ml_functions.mlFunctions import get_cnn_model,img_metadata_generator,get_lstm_model,codes_metadata_generator
from data_ml_functions.dataFunctions import prepare_data,calculate_class_weights
import numpy as np
import os
from keras.layers import merge
from keras.layers.core import Lambda
from keras.models import Model
from keras.callbacks import LearningRateScheduler
import keras.backend
import tensorflow as tf

def make_parallel(model, gpu_count):
    def get_slice(data, idx, parts):
        shape = tf.shape(data)
        size = tf.concat([ shape[:1] // parts, shape[1:] ],axis=0)
        stride = tf.concat([ shape[:1] // parts, shape[1:]*0 ],axis=0)
        start = stride * idx
        return tf.slice(data, start, size)

    outputs_all = []
    for i in range(len(model.outputs)):
        outputs_all.append([])

    #Place a copy of the model on each GPU, each getting a slice of the batch
    for i in range(gpu_count):
        with tf.device('/gpu:%d' % i):
            with tf.name_scope('tower_%d' % i) as scope:

                inputs = []
                #Slice each input into a piece for processing on this GPU
                for x in model.inputs:
                    input_shape = tuple(x.get_shape().as_list())[1:]
                    slice_n = Lambda(get_slice, output_shape=input_shape, arguments={'idx':i,'parts':gpu_count})(x)
                    inputs.append(slice_n)                

                outputs = model(inputs)
                
                if not isinstance(outputs, list):
                    outputs = [outputs]
                
                #Save all the outputs for merging back together later
                for l in range(len(outputs)):
                    outputs_all[l].append(outputs[l])

    # merge outputs on CPU
    with tf.device('/cpu:0'):
        merged = []
        for outputs in outputs_all:
            merged.append(merge(outputs, mode='concat', concat_axis=0))
            
        return Model(input=model.inputs, output=merged)

#from data_ml_functions.multi_gpu import make_parallel

from concurrent.futures import ProcessPoolExecutor
from tqdm import tqdm

import time

class FMOWBaseline:
    def __init__(self, params=None, argv=None):
        """
        Initialize baseline class, prepare data, and calculate class weights.
        :param params: global parameters, used to find location of the dataset and json file
        :return: 
        """
        self.params = params
        
        for arg in argv:
            if arg == '-prepare':
                folder = argv[1].split('/')[-1]
                walkDirs = ['train', 'val', folder]
                prepare_data(params,walkDirs)
                calculate_class_weights(params)
            performAll = (arg == '-all')
            if performAll or arg == '-cnn':
                self.params.train_cnn = True
            if performAll or arg == '-codes':
                self.params.generate_cnn_codes = True
            if performAll or arg == '-lstm':
                self.params.train_lstm = True
            if performAll or arg == '-test':
                self.params.test_cnn = True
                self.params.test_lstm = True
            if performAll or arg == '-test_cnn':
                self.params.test_cnn = True
            if performAll or arg == '-test_lstm':
                self.params.test_lstm = True
            
            if arg == '-nm':
                self.params.use_metadata = False
                
        if self.params.use_metadata:
            self.params.files['cnn_model'] = os.path.join(self.params.directories['cnn_models'], 'cnn_image_and_metadata.model')
            self.params.files['lstm_model'] = os.path.join(self.params.directories['lstm_models'], 'lstm_image_and_metadata.model')
            self.params.files['cnn_codes_stats'] = os.path.join(self.params.directories['working'], 'cnn_codes_stats_with_metadata.json')
            self.params.files['lstm_training_struct'] = os.path.join(self.params.directories['working'], 'lstm_training_struct_with_metadata.json')
            self.params.files['lstm_test_struct'] = os.path.join(self.params.directories['working'], 'lstm_test_struct_with_metadata.json')
        else:
            self.params.files['cnn_model'] = os.path.join(self.params.directories['cnn_models'], 'cnn_model_no_metadata.model')
            self.params.files['lstm_model'] = os.path.join(self.params.directories['lstm_models'], 'lstm_model_no_metadata.model')
            self.params.files['cnn_codes_stats'] = os.path.join(self.params.directories['working'], 'cnn_codes_stats_no_metadata.json')
            self.params.files['lstm_training_struct'] = os.path.join(self.params.directories['working'], 'lstm_training_struct_no_metadata.json')
            self.params.files['lstm_test_struct'] = os.path.join(self.params.directories['working'], 'lstm_test_struct_no_metadata.json')
    
    def train_cnn(self):
        """
        Train CNN with or without metadata depending on setting of 'use_metadata' in params.py.
        :param: 
        :return: 
        """
        
        trainData = json.load(open(self.params.files['training_struct']))
        metadataStats = json.load(open(self.params.files['dataset_stats']))
        #model = load_model(self.params.files['cnn_model'])
        
        model = get_cnn_model(self.params)
        model = make_parallel(model, 4)
        #model = make_parallel(model, 2)
        #model.compile(optimizer=Adam(lr=self.params.cnn_adam_learning_rate), loss='categorical_crossentropy', metrics=['accuracy'])
        model.compile(optimizer=SGD(lr=0.01), loss='categorical_crossentropy', metrics=['accuracy'])
        def scheduler(epoch):
            print("current epoch: ", epoch)
            print("current lr {}".format(keras.backend.get_value(model.optimizer.lr)))
            if epoch%5==0 and epoch!=0:
                lr = keras.backend.get_value(model.optimizer.lr)
                keras.backend.set_value(model.optimizer.lr, lr*0.1)
                print("lr changed to {}".format(lr*.01))
            return keras.backend.get_value(model.optimizer.lr)
        train_datagen = img_metadata_generator(self.params, trainData, metadataStats,aug=True)

        print("training")
        filePath = os.path.join(self.params.directories['cnn_checkpoint_weights'], 'weights.{epoch:02d}.hdf5')
        checkpoint = ModelCheckpoint(filepath=filePath, monitor='loss', verbose=0, save_best_only=False,
                                     save_weights_only=False, mode='auto', period=1)
        
        lr_decay = LearningRateScheduler(scheduler)
        callbacks_list = [checkpoint,lr_decay]

        model.fit_generator(train_datagen,
                            steps_per_epoch=(len(trainData) / self.params.batch_size_cnn + 1),
                            epochs=self.params.cnn_epochs, callbacks=callbacks_list)
       
        model.save(self.params.files['cnn_model'])
        keras.backend.clear_session() 
    def train_lstm(self):
        """
        Train LSTM pipeline using pre-generated CNN codes.
        :param: 
        :return: 
        """

        codesTrainData = json.load(open(self.params.files['lstm_training_struct']))
        codesStats = json.load(open(self.params.files['cnn_codes_stats']))
        metadataStats = json.load(open(self.params.files['dataset_stats']))
        
        model = get_lstm_model(self.params, codesStats)
        model = make_parallel(model, 2)
        model.compile(optimizer=Adam(lr=self.params.lstm_adam_learning_rate), loss='categorical_crossentropy', metrics=['accuracy'])

        train_datagen = codes_metadata_generator(self.params, codesTrainData, metadataStats, codesStats)
        
        print("training")
        filePath = os.path.join(self.params.directories['lstm_checkpoint_weights'], 'weights.{epoch:02d}.hdf5')
        checkpoint = ModelCheckpoint(filepath=filePath, monitor='loss', verbose=0, save_best_only=False, save_weights_only=False, mode='auto', period=1)
        callbacks_list = [checkpoint]

        model.fit_generator(train_datagen,
                            steps_per_epoch=(len(codesTrainData) / self.params.batch_size_lstm + 1),
                            epochs=100, callbacks=callbacks_list,
                            max_queue_size=20)

        model.save(self.params.files['lstm_model'])
        keras.backend.clear_session() 
    def generate_cnn_codes(self):
        """
        Use trained CNN to generate CNN codes/features for each image or (image, metadata) pair
        which can be used to train an LSTM.
        :param: 
        :return: 
        """
        
        metadataStats = json.load(open(self.params.files['dataset_stats']))
        trainData = json.load(open(self.params.files['training_struct']))
        testData = json.load(open(self.params.files['test_struct']))
        cnnModel = load_model(self.params.files['cnn_model'])
        #cnnModel = get_cnn_model(self.params)
        featuresModel = Model(cnnModel.inputs, cnnModel.layers[-6].output)
        
        allTrainCodes = []
        
        featureDirs = ['train', 'test']

        for featureDir in featureDirs:
            
            codesData = {}
            
            isTrain = (featureDir == 'train')
            index = 0

            if isTrain:
                data = trainData
            else:
                data = testData
            
            outDir = os.path.join(self.params.directories['cnn_codes'], featureDir)
            if not os.path.isdir(outDir):
                os.mkdir(outDir)

            N = len(data)
            initBatch = True
            for i,currData in enumerate(tqdm(data)):
                if initBatch:
                    if N-i < self.params.batch_size_eval:
                        batchSize = 1
                    else:
                        batchSize = self.params.batch_size_eval
                    imgdata = np.zeros((batchSize, self.params.target_img_size[0], self.params.target_img_size[1], self.params.num_channels))
                    metadataFeatures = np.zeros((batchSize, self.params.metadata_length))
                    batchIndex = 0
                    tmpBasePaths = []
                    tmpFeaturePaths = []
                    tmpCategories = []
                    initBatch = False

                path,_  = os.path.split(currData['img_path'])
                if isTrain:
                    basePath = path[len(self.params.directories['train_data'])+1:]
                else:
                    basePath = path[len(self.params.directories['test_data'])+1:]
                    
                tmpBasePaths.append(basePath)
                if isTrain:
                    tmpCategories.append(currData['category'])
                
                origFeatures = np.array(json.load(open(currData['features_path'])))
                tmpFeaturePaths.append(currData['features_path'])

                metadataFeatures[batchIndex, :] = np.divide(origFeatures - np.array(metadataStats['metadata_mean']), metadataStats['metadata_max'])
                imgdata[batchIndex,:,:,:] = image.img_to_array(image.load_img(currData['img_path']))

                batchIndex += 1

                if batchIndex == batchSize:
                    imgdata = imagenet_utils.preprocess_input(imgdata)
                    imgdata = imgdata / 255.0

                    if self.params.use_metadata:
                        cnnCodes = featuresModel.predict([imgdata,metadataFeatures], batch_size=batchSize)
                    else:
                        cnnCodes = featuresModel.predict(imgdata, batch_size=batchSize)

                    for codeIndex,currCodes in enumerate(cnnCodes):
                        currBasePath = tmpBasePaths[codeIndex]
                        outFile = os.path.join(outDir, '%07d.json' % index)
                        index += 1
                        json.dump(currCodes.tolist(), open(outFile, 'w'))
                        if currBasePath not in codesData.keys():
                            codesData[currBasePath] = {}
                            codesData[currBasePath]['cnn_codes_paths'] = []
                            if self.params.use_metadata:
                                codesData[currBasePath]['metadata_paths'] = []
                            if isTrain:
                                codesData[currBasePath]['category'] = tmpCategories[codeIndex]
                        codesData[currBasePath]['cnn_codes_paths'].append(outFile)
                        if self.params.use_metadata:
                            codesData[currBasePath]['metadata_paths'].append(tmpFeaturePaths[codeIndex])
                        if isTrain:
                            allTrainCodes.append(currCodes)
                        initBatch = True
        
            if isTrain:
                codesTrainData = codesData
            else:
                codesTestData = codesData

        N = len(allTrainCodes[0])
        sumCodes = np.zeros(N)
        for currCodes in allTrainCodes:
            sumCodes += currCodes
        avgCodes = sumCodes / len(allTrainCodes)
        maxCodes = np.zeros(N)
        for currCodes in allTrainCodes:
            maxCodes = np.maximum(maxCodes, np.abs(currCodes-avgCodes))
        maxCodes[maxCodes == 0] = 1
            
        maxTemporal = 0
        for key in codesTrainData.keys():
            currTemporal = len(codesTrainData[key]['cnn_codes_paths'])
            if currTemporal > maxTemporal:
                maxTemporal = currTemporal

        codesStats = {}
        codesStats['codes_mean'] = avgCodes.tolist()
        codesStats['codes_max'] = maxCodes.tolist()
        codesStats['max_temporal'] = maxTemporal

        json.dump(codesTrainData, open(self.params.files['lstm_training_struct'], 'w'))
        json.dump(codesStats, open(self.params.files['cnn_codes_stats'], 'w'))
        json.dump(codesTestData, open(self.params.files['lstm_test_struct'], 'w'))

    def test_models(self):

        #codesTestData = json.load(open(self.params.files['lstm_test_struct']))
        metadataStats = json.load(open(self.params.files['dataset_stats']))
    
        metadataMean = np.array(metadataStats['metadata_mean'])
        metadataMax = np.array(metadataStats['metadata_max'])
        print(self.params.files['cnn_model'])
        cnnModel = load_model(self.params.files['cnn_model'],custom_objects={"tf": tf})
        #cnnModel = load_model("/home/jmsun/kaggle/baseline/data/working/cnn_models/cnn_image_and_metadata.model.299", custom_objects={"tf": tf})
        cnnModel = cnnModel.layers[-2]
        #cnnModel = get_cnn_model(self.params)
        
        if self.params.test_lstm:
            codesStats = json.load(open(self.params.files['cnn_codes_stats']))
            lstmModel = load_model(self.params.files['lstm_model'])
            #lstmModel = get_lstm_model(self.params, codesStats)

        index = 0
        timestr = time.strftime("%Y%m%d-%H%M%S")
        
        if self.params.test_cnn:
            fidCNN = open(os.path.join(self.params.directories['predictions'], 'predictions-cnn-%s.txt' % timestr), 'w')
        if self.params.test_lstm:
            fidLSTM = open(os.path.join(self.params.directories['predictions'], 'predictions-lstm-%s.txt' % timestr), 'w')

        def walkdir(folder):
            for root, dirs, files in os.walk(folder):
                if len(files) > 0:
                    yield (root, dirs, files)
        
        num_sequences = 0
        for _ in walkdir(self.params.directories['test_data']):
            num_sequences += 1
        import pandas as pd
        preds = []
        bbids = [] 
        for root, dirs, files in tqdm(walkdir(self.params.directories['test_data']), total=num_sequences):
            if len(files) > 0:
                imgPaths = []
                metadataPaths = []
                slashes = [i for i,ltr in enumerate(root) if ltr == '/']
                bbID = int(root[slashes[-1]+1:])

            for file in files:
                if file.endswith('.jpg'):
                    imgPaths.append(os.path.join(root,file))
                    metadataPaths.append(os.path.join(root, file[:-4]+'_features.json'))
                    
            if len(files) > 0:
                inds = []
                for metadataPath in metadataPaths:
                    underscores = [ind for ind,ltr in enumerate(metadataPath) if ltr == '_']
                    inds.append(int(metadataPath[underscores[-3]+1:underscores[-2]]))
                inds = np.argsort(np.array(inds)).tolist()
                
                currBatchSize = len(inds)
                imgdata = np.zeros((currBatchSize, self.params.target_img_size[0], self.params.target_img_size[1], self.params.num_channels))
                metadataFeatures = np.zeros((currBatchSize, self.params.metadata_length))

                codesIndex = 0
                #codesPaths = codesTestData[root[24:]]
                codesFeatures = []
                for ind in inds:
                    img = image.load_img(imgPaths[ind])
                    img = image.img_to_array(img)
                    img.setflags(write=True)
                    imgdata[ind,:,:,:] = img

                    features = np.array(json.load(open(metadataPaths[ind])))
                    features = np.divide(features - metadataMean, metadataMax)
                    metadataFeatures[ind,:] = features
                    #codesFeatures.append(json.load(open(codesPaths['cnn_codes_paths'][codesIndex])))
                    codesIndex += 1

                imgdata = imagenet_utils.preprocess_input(imgdata)
                imgdata = imgdata / 255.0
                from scipy.stats.mstats import gmean 
                if self.params.test_cnn:
                    if self.params.use_metadata:
                        #predictionsCNN = np.sum(cnnModel.predict([imgdata, metadataFeatures], batch_size=currBatchSize), axis=0)
                        #predictionsCNN = gmean(cnnModel.predict([imgdata, metadataFeatures], batch_size=currBatchSize), axis=0)
                        predictionsCNN = cnnModel.predict([imgdata, metadataFeatures], batch_size=currBatchSize)
                    else:
                        predictionsCNN = np.sum(cnnModel.predict(imgdata, batch_size=currBatchSize), axis=0)
                
                if self.params.test_lstm:
                    if self.params.use_metadata:
                        codesMetadata = np.zeros((1, codesStats['max_temporal'], self.params.cnn_seq2seq_layer_length+self.params.metadata_length))
                    else:
                        codesMetadata = np.zeros((1, codesStats['max_temporal'], self.params.cnn_seq2seq_layer_length))

                    timestamps = []
                    for codesIndex in range(currBatchSize):
                        cnnCodes = codesFeatures[codesIndex]
                        timestamp = (cnnCodes[4]-1970)*525600 + cnnCodes[5]*12*43800 + cnnCodes[6]*31*1440 + cnnCodes[7]*60
                        timestamps.append(timestamp)
                        cnnCodes = np.divide(cnnCodes - np.array(codesStats['codes_mean']), np.array(codesStats['codes_max']))
                        codesMetadata[0,codesIndex,:] = cnnCodes
                    
                    sortedInds = sorted(range(len(timestamps)), key=lambda k:timestamps[k])
                    codesMetadata[0,range(len(sortedInds)),:] = codesMetadata[0,sortedInds,:]

                    predictionsLSTM = lstmModel.predict(codesMetadata, batch_size=1)
                
            if len(files) > 0:
                if self.params.test_cnn:
                    for idx in range(predictionsCNN.shape[0]):
                        preds.append(predictionsCNN[idx])
                        bbids.append(bbID)
                    predCNN = np.argmax(np.sum(predictionsCNN,axis=0))
                    oursCNNStr = self.params.category_names[predCNN]
                    fidCNN.write('%d,%s\n' % (bbID,oursCNNStr))
                if self.params.test_lstm:
                    preds.append(predictionsLSTM[0])
                    bbids.append(bbID)
                    predLSTM = np.argmax(predictionsLSTM[0])
                    predLSTMmax = np.max(predictionsLSTM)
                    oursLSTMStr = self.params.category_names[predLSTM]
                    fidLSTM.write('%d,%s\n' % (bbID,oursLSTMStr))
                index += 1
                
        if self.params.test_cnn:            
            fidCNN.close()
        if self.params.test_lstm:
            fidLSTM.close()
        df = pd.DataFrame(np.array(preds))
        df.columns = self.params.category_names
        df['_id'] = bbids
        df.to_csv('baseline_cnn_299_probs.csv',index=False)    
        keras.backend.clear_session()            
    
    
