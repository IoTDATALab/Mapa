import torch
import torch.nn as nn
import torch.utils.data as Data
from torch.utils.data import Dataset
import torch.nn.functional as F
import paho.mqtt.client as mqtt
import _pickle as cPickle
import numpy as np
import os, queue, random, math
import matplotlib.pyplot as plt
import json
import pickle
import collections

#import logging
#logging.basicConfig(level=logging.DEBUG)
#logger = logging.getLogger(__name__)

CLIENT_ID = str(random.random())
EPOCH = int(os.environ.get('EPOCH'))
MQTT_PORT = int(os.environ.get('MQTT_PORT'))
MQTT_IP = os.environ.get('MQTT_IP')
TEST_NUM = int(os.environ.get('TEST_NUM'))
RESULT_ROOT = os.environ.get('RESULT_ROOT')
EDGE_NAME = os.environ.get('EDGE_NAME')
SPLIT = int(os.environ.get('SPLIT'))

isend = False
z = 0.8
Grad_Upper = 2.0
BATCH_SIZE = 5


class Reddit(Dataset):
    def __init__(self, data_root, vocab_size, vocab=None):
    
        self.users = 0
        self.num_samples = []
        self.data = []
        self.targets = []

        if vocab == None:
            counter = None
            for r in data_root:
                with open(r) as file:
                    js = json.load(file)
                   
                    counter = self.build_counter(js['user_data'], initial_counter=counter)

            if counter is not None:
                self.vocab = self.build_vocab(counter, vocab_size=vocab_size)
            else:
                print('No files to process.')
        else:
            self.vocab = vocab

        for r in data_root:
            
            with open(r) as file:
                js = json.load(file)
                #self.num_samples += js['num_samples']
                for u in js['users']:
                    self.users += 1
                    if (self.users <= 280 * (SPLIT + 1)) and (self.users > 280 * SPLIT):
                        for d in js['user_data'][u]['x']:
                            for dd in d:
                                self.data.append(self.word_to_indices(dd))
                        for t in js['user_data'][u]['y']:
                            for tt in t['target_tokens']:
                                self.targets.append(self.word_to_indices(tt))
        print(len(self.data))

        self.data = torch.tensor(self.data)
        self.targets = torch.tensor(self.targets)

    def letter_to_index(self, letter):
        '''returns one-hot representation of given letter
        '''
        if letter in self.vocab.keys():
            index = self.vocab[letter]
        else:
            index = 1
        return index

    def word_to_indices(self, word):
        indices = []
        for c in word:
            if c in self.vocab.keys():
                indices.append(self.vocab[c])
            else:
                indices.append(1)

        return indices

    def build_counter(self, train_data, initial_counter=None):
        all_words = []
        for u in train_data:
            for c in train_data[u]['x']:
                for s in c:
                    all_words += s

        if initial_counter is None:
            counter = collections.Counter()
        else:
            counter = initial_counter
        counter.update(all_words)

        return counter

    def build_vocab(self, counter, vocab_size=1000):
        count_pairs = sorted(counter.items(),
                             key=lambda x: (-x[1], x[0])) 
        count_pairs = count_pairs[:(vocab_size - 2)]

        words, counters = list(zip(*count_pairs)) 

        vocab = {}
        vocab['<PAD>'] = 0
        vocab['<UNK>'] = 1

        for i, w in enumerate(words):
            if w != '<PAD>':
                vocab[w] = i + 1

        return vocab

    def __len__(self):
        return len(self.targets)

    def __getitem__(self, idx):
        data = self.data[idx]
        target = self.targets[idx]
        return data, target


data_root_train = ['./data/reddit/train/train_data.json']
data_root_test = ['./data/reddit/test/test_data.json']

train_data = Reddit(data_root_train, vocab_size=1000)
test_data = Reddit(data_root_test, vocab_size=1000, vocab=train_data.vocab)

train_loader = Data.DataLoader(dataset=train_data, batch_size=BATCH_SIZE, shuffle=True)
test_loader = Data.DataLoader(dataset=test_data, batch_size=BATCH_SIZE, shuffle=True)


class LSTM(nn.Module):
    def __init__(self, vocab_size=1000, embedding_dim=200, hidden_dim=256, num_layers=1):
        super(LSTM, self).__init__()
        self.hidden_dim = hidden_dim
        self.num_layer = num_layers
        self.embeddings = nn.Embedding(vocab_size, embedding_dim)
        self.lstm = nn.LSTM(embedding_dim, self.hidden_dim, num_layers=self.num_layer, batch_first=True)
        self.linear = nn.Linear(self.hidden_dim, vocab_size)



    def forward(self, input, hidden=None):
        batch_size, seq_len = input.size()
        if hidden is None:
            h_0 = torch.zeros(self.num_layer, batch_size, self.hidden_dim)
            c_0 = torch.zeros(self.num_layer, batch_size, self.hidden_dim)
        else:
            h_0, c_0 = hidden

        embeds = self.embeddings(input)
        output, _ = self.lstm(embeds, (h_0, c_0))  #output size is [batch_size, seq_len, hidden_dim]
        output = self.linear(output.reshape(batch_size*seq_len,-1))   #output size is [batch_size, seq_len, vocab_size]        

        return output


model = LSTM()

def GetModelLayers(params):
    Layer_nodes = []
    Layer_shape = []
    for i in range(len(params)):
        Layer_nodes.append(params[i].numel())
        Layer_shape.append(params[i].shape)
    return Layer_nodes, Layer_shape


def ClipBound_gerate(Grad_Upper, Layer_nodes, style="Flat"):

    if style == "Flat":

        Clip_bound=torch.Tensor([Grad_Upper])

    if style == "Per-Layer":

        Layer_nodes = torch.Tensor(Layer_nodes).float()

        Clip_bound = Layer_nodes/ Layer_nodes.norm() * Grad_Upper

    #Clip_bound = torch.Tensor(Clip_bound)

       

    return Clip_bound


def init_grads(params):
    grads = []
    for i in range(len(params)):
        grads.append(torch.zeros(params[i].shape))

    return grads

#grads_clip
def Clip(grads, Clip_bound):
    if len(Clip_bound) == 1:
        norm = torch.tensor([0.])
        for i in range(len(grads)):
            norm += grads[i].float().norm()**2
        norm = norm.sqrt()

        for i in range(len(grads)):
            grads[i] = grads[i] * torch.min(torch.ones(1), Clip_bound[0] / norm)

    if len(Clip_bound) > 1:
        for i in range(len(grads)):
            grads[i] = grads[i] * torch.min(torch.ones(1), Clip_bound[i] / grads[i].float().norm())

    return grads


def Add_noise(grads, Clip_bound):
    std = z
    if len(Clip_bound) == 1:
        for i in range(len(grads)):
            grads[i] = grads[i] + std * Clip_bound[0] * torch.randn(grads[i].shape)

    if len(Clip_bound) > 1:
        for i in range(len(grads)):
            grads[i] = grads[i] + std * Clip_bound[i] * torch.randn(grads[i].shape)

    return grads


msgQueue=queue.Queue()

def on_connect(mqttc, obj, flags, rc):
    print("rc: " + str(rc))
def on_message(mqttc, obj, msg):
    #print("received: " + msg.topic + " " + str(msg.qos))
    if msg.topic == "Halt":
        global isend
        mqttc.unsubscribe("fixdp_params/"+ CLIENT_ID)#
        msgQueue.queue.clear()
        isend = True
    else:
        msgQueue.put(msg.payload)



client = mqtt.Client(client_id=CLIENT_ID)
#client.enable_logger(logger)
client.on_connect = on_connect
client.on_message = on_message
client.connect(MQTT_IP, MQTT_PORT, 600)
client.subscribe([("init", 2), ("fixdp_params/" + CLIENT_ID, 2), ("Halt",2)])
client.loop_start()

if __name__ == '__main__':

    test_idx = 1000
   
    params = cPickle.loads(msgQueue.get())
    
  
    
    Layer_nodes, Layer_shape = GetModelLayers(params)
       
    Clip_bound = ClipBound_gerate(Grad_Upper, Layer_nodes, style="Flat")
    
    for i, f in enumerate(model.parameters()):
        f.data = params[i].float()


    step = 0
    for epoch in range(EPOCH):

        train_loss = 0
        for i, (data, target) in enumerate(train_loader):

            if isend:
                break
              
            grads = init_grads(params)
            loss = 0
            for idx in range(data.size(0)):
                inner_data = torch.unsqueeze(data[idx], 0)
                inner_target = torch.unsqueeze(target[idx],0)

               
                model.zero_grad()
                output = model(inner_data)
                output = output.view([-1, 1000])
                per_loss = nn.CrossEntropyLoss()(output, inner_target.view(-1))
                per_loss.backward()

                
                loss += per_loss.item()
                per_grads = []
                for params in model.parameters():
                    per_grads.append(params.grad.data)

                
                per_grads = Clip(per_grads, Clip_bound)


                for i in range(len(grads)):
                    grads[i] += per_grads[i]

            
            for i in range(len(grads)):
                grads[i] /= BATCH_SIZE
            loss /= BATCH_SIZE
           
            
            train_loss += loss
                        
            grads_noise = Add_noise(grads, Clip_bound / BATCH_SIZE)

            
            client.publish("fixdp_grads/" + CLIENT_ID, cPickle.dumps(grads_noise), 2)



           
            if step % TEST_NUM == 0:
                print(step)
                total = 0
                correct = 0
                correct_pad = 0
                test_loss = 0
                man_file1 = open(RESULT_ROOT + '[' + str(EDGE_NAME) + ']' + '[FixedDP-Accuracy]' + '.txt', 'a')
                for batch_idx, (test_x, test_y) in enumerate(test_loader):
                    if batch_idx < test_idx : 
                        
                        
                        output = model(test_x)
                        pred_y = torch.max(output, 1)[1].data.numpy()
                        test_y = test_y.view(-1)
                        test_loss += nn.CrossEntropyLoss()(output, test_y).item()
                        total += float(test_y.size(0))
                        
                        correct += float((pred_y == test_y.data.numpy()).sum())
                        correct_pred = (pred_y == test_y.data.numpy())
                        
                        pad = np.zeros(test_y.size(0))
                        pad_pred = (pred_y == pad)
                        correct_pad += float((correct_pred*pad_pred).sum())
                      
                    else:
                        break

               
                test_loss /= test_idx

                accuracy = (correct - correct_pad) / total
                man_file1.write(str(accuracy) + "\n")
                man_file1.close()

                man_file2 = open(RESULT_ROOT + '[' + str(EDGE_NAME) + ']' + '[FixedDP-TestLoss]' + '.txt', 'a')
                man_file2.write(str(test_loss) + "\n")
                man_file2.close()

                man_file3 = open(RESULT_ROOT + '[' + str(EDGE_NAME) + ']' + '[FixedDP-TrainLoss]' + '.txt', 'a')
                if step != 0:
                    train_loss /= TEST_NUM
                man_file3.write(str(train_loss) + "\n")
                man_file3.close()
                train_loss = 0

            step += 1
                        
            params = cPickle.loads(msgQueue.get())
            for i, f in enumerate(model.parameters()):
                f.data = params[i].float()

        if isend:
            break
