import torch
import syft as sy  # <-- NEW: import the Pysyft library
import random
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torchvision import datasets, transforms
from datetime import datetime
from torch.autograd import Variable
import ComputePrivacy as Privacy # Import self definition function to compute the privacy loss
import Datasets # Import self definition function to load the federated datasets
import os
hook = sy.TorchHook(torch)  # <-- NEW: hook PyTorch ie add extra functionalities to support Federated Learning
date = datetime.now().strftime('%Y-%m-%d %H:%M')

# Define parameters
class Arguments():
    def __init__(self):
        self.batch_size = 0.03 # Number of samples used of each user/device at each iteration.
        # If this value is less than 1, then it means the sampling ratio, else it means the mini-batch size
        self.lr = 0.1  # Learning rate, is adaptively adjusted
        self.L_smooth = 3 * 10 ** -3   # L smooth constant of gradient
        self.grad_var = 5  # Variance of gradients on samples
        self.grad_upper_bound = torch.tensor([2.])  # clipbound
        self.redu_ratio = torch.tensor(0.8)   # Reductio ratio
        self.inital_bound = 1   # Initial estimation of f(x_1)-f(x^*)
        self.z = 0.4  # Noise parameter z in Gaussian noise N(0, (zS)^2) where S is sensitivity
        self.users_total = 817  # Total number of users/devices
        self.user_sel_prob = 0.01  # Probability for sampling users/devices at each iteration
        self.itr_numbers = 1500  # Number of total iterations

        self.test_batch_size = 3  # Number of test mini-batch size
        self.log_train = 30  # Logging interval for printing the training loss
        self.log_test = 30  # Logging interval for printing the test accuracy
        self.save_model = False
        self.batchs_round = 1  # Number of mini-batchs of each selected user in each iteration
        self.no_cuda = True
        self.seed = 1
        self.ClipStyle = 'Flat'  # Clipping method, including Flat and Per-Layer

        self.vocab_size = 1000
        self.hidden_size = 128
        self.embed_size = 200
        self.test_batch_num = 500


args = Arguments()

use_cuda = not args.no_cuda and torch.cuda.is_available()
torch.manual_seed(args.seed)
device = torch.device("cuda" if use_cuda else "cpu")
kwargs = {'num_workers': 1, 'pin_memory': True} if use_cuda else {}

# Define model
class Net(nn.Module):
    def __init__(self, input_size=1000, embed_size=200, hidden_size=256, output_size=1000, num_layer=2):
        super(Net, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.num_layer = num_layer
        self.embedding = nn.Embedding(input_size, embed_size)
        self.lstm = nn.LSTM(input_size=embed_size, hidden_size=hidden_size, num_layers=num_layer, batch_first=True)
        self.hidden2tag = nn.Linear(hidden_size, output_size)
        #self.hidden = self.init_hidden()

    def forward(self, input):
        input = Variable(input)
        decoder = self.embedding(input)
        lstm_out, hidden = self.lstm(decoder)
        output = self.hidden2tag(lstm_out)
        # output = F.softmax(output, dim=1)
        # output = self.linear(output.contiguous().view(output.shape[0], -1))
        return output

    def init_hidden(self,batch_size):
        return (torch.autograd.Variable(torch.zeros(self.num_layer, batch_size, self.hidden_size)),
                torch.autograd.Variable(torch.zeros(self.num_layer, batch_size, self.hidden_size)))


###################################################################################
############################### Define functions ##################################

################### Define split of users/devices ##############
def Virtual_Users_num(Leaf_split, LEAF=True):
    if LEAF:
        Users_num_total = len(Leaf_split)
        Ratio = Leaf_split
    else:
        Users_num_total = args.users_total
        Ratio = [random.randint(1, 10) for _ in range(Users_num_total)]
    return Users_num_total, Ratio

############ Obtain the number and shape of layers of the model ##########
def GetModelLayers(model):
    Layers_shape = []
    Layers_nodes = []
    for idx, params in enumerate(model.parameters()):
        Layers_num = idx
        Layers_shape.append(params.shape)
        Layers_nodes.append(params.numel())
    return Layers_num + 1, Layers_shape, Layers_nodes

######################## Initialize all layers as zero ###################
def ZerosGradients(Layers_shape):
    ZeroGradient = []
    for i in range(len(Layers_shape)):
        ZeroGradient.append(torch.zeros(Layers_shape[i]))
    return ZeroGradient

################## Define Clipping bound for Flat/Per-Layer ###############
def ClipBound_gerate(Clipbound, Layers_nodes, style="Flat"):
    Layer_nodes = torch.tensor(Layers_nodes).float()
    if style == "Flat":
        ClipBound = Clipbound
    if style == "Per-Layer":
        ClipBound = Layer_nodes/Layer_nodes.norm() * Clipbound
    return ClipBound

####### Define the learning rate and number of iterations per stage ########
def Stage_Lr_Itrs(grad_upper_bound, Layers_nodes, leaf_split):
    # Delta_b consists of variance of sampling and noise
    batch_size = np.mean(leaf_split) * args.batch_size
    Delta_b = args.grad_var**2 / batch_size + sum(Layers_nodes) * (args.z * grad_upper_bound / batch_size)**2
    # Avarage delay
    tau = torch.ceil(torch.tensor(1/ args.user_sel_prob))
    # P
    P = 2 * Delta_b / (args.redu_ratio**2 * grad_upper_bound**2 * (tau + 1))
    # Learning rate
    Stage_Lr = 1 / (2 * max(P, 1) * args.L_smooth * (tau + 1))
    # Number of iterations
    Stage_Iteration = torch.ceil(4 * P**2 * args.L_smooth * (tau + 1)**2 * args.inital_bound / Delta_b)
    return Stage_Lr, Stage_Iteration

################## Define clip ##########################
def TensorClip(Tensor, ClipBound):
    norm = torch.tensor([0.])
    Layers_num = len(Tensor)
    Tuple_num = torch.ones(Layers_num)
    for i in range(Layers_num):
        norm = norm + Tensor[i].float().norm()**2
        Tuple_num[i] = Tensor[i].numel()
    norm = norm.sqrt()
    if len(ClipBound) == 1:
        Indicator = 1*(norm<=ClipBound[0])
        for i in range(Layers_num):
            Tensor[i] = Tensor[i]*torch.min(torch.ones(1), ClipBound[0]/norm)
    if len(ClipBound) > 1:
        Indicator = torch.zeros(Layers_num)
        for i in range(Layers_num):
            norm_layer = Tensor[i].float().norm()
            Indicator[i] = 1*(norm_layer<=ClipBound[i])
            Tensor[i] = Tensor[i]*torch.min(torch.ones(1), ClipBound[i]/norm_layer)
    return Tensor, Indicator

#################### Add noise ######################
def AddNoise(Tensor, Clipboud):
    std = args.z
    if len(Clipboud) == 1:
        for i in range(len(Tensor)):
            Tensor[i] = Tensor[i] + std * Clipboud[0] * torch.randn(Tensor[i].shape)
    if len(Clipboud) > 1:
        for i in range(len(Tensor)):
            Tensor[i] = Tensor[i] + std * Clipboud[i] * torch.randn(Tensor[i].shape)
    return Tensor

############### Define train process #######################
def train(learning_rate, model, train_data, train_target, device, optimizer, gradient=True):
    model.train()
    model.zero_grad()
    #model.hidden = model.init_hidden()
    train_data = Variable(train_data)
    output = model(train_data)
    output = output.view([-1, args.vocab_size])
    train_target = Variable(train_target.view([-1]).long())
    loss = Criteria(output, train_target)
    loss.backward()
    Gradients_Tensor = []
    if gradient == False:
        for params in model.parameters():
            Gradients_Tensor.append(-learning_rate*params.grad.data)
    if gradient == True:
        for params in model.parameters():
            Gradients_Tensor.append(params.grad.data)
    return Gradients_Tensor, loss

################## Define test process #######################
def test(model, device, test_loader):
    model.eval()
    test_loader_len = len(test_loader.dataset)
    test_loss = 0
    correct = 0
    with torch.no_grad():
        for batch_idx, (data, target) in enumerate(test_loader):
            if batch_idx < args.test_batch_num:
                data = torch.squeeze(data)
                target = torch.cat(target)
                data, target = data.to(device), target.to(device)
                output = model(data)
                output = output.view([-1, args.vocab_size])
                target = target.view([-1]).long()
                test_loss += Criteria(output, target) * data.shape[0]  # sum up batch loss
                # get the index of the max log-probability
                y_pred = torch.argmax(output, dim=1)
                # for i in range(0,len(y_pred)):
                # if y_pred[i] == 0:
                # y_pred[i] = -1
                # if y_pred[i] == 1:
                # y_pred[i] = -1
                correct += (y_pred == target).sum()
            else:
                break

        test_loss /= (args.test_batch_num * args.test_batch_size)
        test_acc = correct.float() / (args.test_batch_num * args.test_batch_size * 10)

        print('\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n'.format(
            test_loss, correct, args.test_batch_num * args.test_batch_size * 10,
                                100. * test_acc))
    return test_loss, test_acc

#################################################################
###################### Load Dataset ##############
# Path
data_root_train = ["../data/reddit/train/train_data.json"]
data_root_test = ["../data/reddit/test/test_data.json"]

# Load train datasets
train_loader = Datasets.reddit(data_root_train, args.vocab_size)
Leaf_split = train_loader.num_samples
# Load test datasets
test_loader = torch.utils.data.DataLoader(Datasets.reddit(data_root_test, args.vocab_size),
    batch_size=args.test_batch_size, shuffle=True, **kwargs)

###################################################################################
############## Generate model and users ##############
model = Net().to(device)
workers = []
models = {}
optims = {}
Users_num_total, Ratio = Virtual_Users_num(Leaf_split, LEAF=True)
for i in range(1, Users_num_total+1):
    exec('user{} = sy.VirtualWorker(hook, id="user{}")'.format(i,i))
    exec('models["user{}"] = model.copy()'.format(i))
    exec('optims["user{}"] = optim.SGD(params=models["user{}"].parameters(), lr=args.lr)'.format(i,i))
    exec('workers.append(user{})'.format(i))
optim_sever = optim.SGD(params=model.parameters(),lr=args.lr)
print('THe number of total users: {}, the least samples of users is {}'.format(Users_num_total, min(Leaf_split)))

###################################################################################
########## Assign train dataset to all users/devices ############
Federate_Dataset = Datasets.dataset_federate_noniid(train_loader, workers, Ratio=Ratio)
Leaf_split = [round(len(train_loader)*r/sum(Ratio)) for r in Ratio]
print(len(train_loader.targets),sum(Leaf_split))
#print(train_loader.targets)
print(np.mean(Leaf_split),np.std(Leaf_split))
Criteria = nn.CrossEntropyLoss()

###################################################################################
######## Initial model accuracy on test dataset #######
test_loss, test_acc = test(model, device, test_loader) #

######## Logging files #######
with open('../results/REDDIT/MAPA_S_Asyn_04flat_TestLoss.txt', 'a+') as fl:
    fl.write('\n' + date + ' Results (Total users number is ' + str(Users_num_total)
             + ', batch_size is ' + str(args.batch_size)+ ', z is ' + str(args.z) + ', L_smmoth is ' + str(args.L_smooth)+', style is Flat'  +')\n')
    fl.write(str(test_loss.item()) + '\t')
with open('../results/REDDIT/MAPA_S_Asyn_04flat_TestAcc.txt', 'a+') as fl:
    fl.write('\n' + date + ' Results (Total users number ' + str(Users_num_total)
             + ', batch_size is ' + str(args.batch_size)+ ', z is ' + str(args.z) + ', L_smmoth is ' + str(args.L_smooth)+', style is Flat'  +')\n')
    fl.write(str(test_acc.item()) + '\t')
with open('../results/REDDIT/MAPA_S_Asyn_04flat_TrainLoss.txt', 'a+') as fl:
    fl.write('\n' + date + ' Results (Total users number ' + str(Users_num_total)
             + ', batch_size is ' + str(args.batch_size)+ ', z is ' + str(args.z) + ', L_smmoth is ' + str(args.L_smooth)+', style is Flat'  +')\n')
with open('../results/REDDIT/MAPA_S_Asyn_04flat_Budget.txt', 'a+') as fl:
    fl.write('\n' + date + ' Results (Total users number ' + str(Users_num_total)
             + ', batch_size is ' + str(args.batch_size)+ ', z is ' + str(args.z) + ', L_smmoth is ' + str(args.L_smooth)+', style is Flat'  +')\n')

###################################################################################
############## Federated learning process ##############
# Logging dictionary
logs = {'train_loss': [], 'test_loss': [], 'test_acc': [], 'varepsilon': []}
# Obtain information of layers
Layers_num, Layers_shape, Layers_nodes = GetModelLayers(model)
#  Learning rate and number of iterations per stage
Grad_Upper = args.grad_upper_bound
Stage_Lr, Stage_Iteration = Stage_Lr_Itrs(Grad_Upper, Layers_nodes, Leaf_split)
print(Stage_Iteration)
# Total number of iterations
Stage_Itr_count = Stage_Iteration
## Stage count
Stage_count = 1
print('Stage number: {}, - - -  Stage_Lr = {}, Stage_iterations = {}'.format(Stage_count, Stage_Lr, Stage_Iteration))
# Set clipping bound
Clip_Bound = ClipBound_gerate(Grad_Upper, Layers_nodes, style=args.ClipStyle)
print(Clip_Bound)

# Compute the sampling ratio for moment account
Sampled_ratio = args.user_sel_prob * args.batch_size
delta = 1. / sum(Leaf_split) ** 1.1 # delta in (varepsilon, delta)-DP
#Privac loss of the first iteraiton
varepsilon = Privacy.ComputePrivacy(Sampled_ratio, args.z, 1, delta, 32)
logs['varepsilon'].append(varepsilon)

with open('../results/REDDIT/MAPA_S_Asyn_04flat_Budget.txt', 'a+') as fl:
    fl.write(str(varepsilon) + '\t')

# Define train/test process
for itr in range(1, args.itr_numbers + 1):
    print(itr)
    # Select the participants from the total users with the given probability
    Users_Current = np.random.binomial(Users_num_total, args.user_sel_prob, 1).sum()

    # Load samples from the participants with the given probability or mini-batch size args.batch_size
    federated_train_loader = sy.FederatedDataLoader(Federate_Dataset, batch_size=args.batch_size, shuffle=True,
                                                    worker_num=Users_Current, batch_num=args.batchs_round, **kwargs)
    workers_list = federated_train_loader.workers # List of participants at the current iteration

    # Next two lines are only necessary for synchronous aggregation
    # for idx in range(len(workers_list)):
    #     models[workers_list[idx]] = model

    # Initialize the same model-structure tensor with zero elements
    Collect_Gradients = ZerosGradients(Layers_shape)
    Loss_train = torch.tensor(0.)
    for idx_outer, (train_data, train_targets) in enumerate(federated_train_loader):
        model_round = models[train_data.location.id]
        optimizer = optims[train_data.location.id]
        train_data, train_targets = train_data.to(device), train_targets.to(device)
        train_data, train_targets = train_data.get(), train_targets.get()

        Collect_Gradients_Per_user = ZerosGradients(Layers_shape)
        Loss_Per_user = torch.tensor(0.)
        ## Clip gradient per sample
        for inner_dex in range(train_data.size(0)):
            inner_data = torch.unsqueeze(train_data[inner_dex], 0)
            inner_target = torch.unsqueeze(train_targets[inner_dex], 0)
            Gradients_Sample, Loss_Sample = train(Stage_Lr, model_round, inner_data, inner_target, device, optimizer,
                                           gradient=True)
            # Clip
            Gradients_Sample, _ = TensorClip(Gradients_Sample, Clip_Bound)
            # Accumulation of loss of samples
            Loss_Per_user += Loss_Sample
            # Aggregation of gradients of a mini-batch
            for i in range(Layers_num):
                Collect_Gradients_Per_user[i] += Gradients_Sample[i]

        # Aggregation of gradients of a mini-batch
        for i in range(Layers_num):
            Collect_Gradients_Per_user[i] /= (inner_dex + 1)
        Loss_Per_user /= (inner_dex + 1)
        # Add noise
        Collect_Gradients_Per_user = AddNoise(Collect_Gradients_Per_user, Clip_Bound / (inner_dex + 1))
        # Accumulation of loss of the current participants
        Loss_train += Loss_Per_user

        # Accumulation of gradients of the current participants
        for i in range(Layers_num):
            Collect_Gradients[i] += Collect_Gradients_Per_user[i]
    # Update model using the average gradient of k users/devices
    for grad_idx, params_sever in enumerate(model.parameters()):
        params_sever.data.add_(-Stage_Lr * Collect_Gradients[grad_idx] / (idx_outer + 1))
    # Average loss
    Loss_train /= (idx_outer + 1)

    # Send the updated model to the corresponding participants, only for asynchronous aggregation
    for worker_idx in range(len(workers_list)):
        worker_model = models[workers_list[worker_idx]]
        for _, (params_server, params_client) in enumerate(zip(model.parameters(),worker_model.parameters())):
            params_client.data = params_server.data
        models[workers_list[worker_idx]] = worker_model

    ############ Print train loss #################
    if itr == 1 or itr % args.log_train == 0:
        print('Train Iteration: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
            itr, itr, args.itr_numbers,
            100. * itr / args.itr_numbers, Loss_train.item()))
        logs['train_loss'].append(Loss_train.item())
        with open('../results/REDDIT/MAPA_S_Asyn_04flat_TrainLoss.txt', 'a+') as fl:
            fl.write(str(Loss_train.item()) + '\t')

    ############ Print test loss and test acc #################
    if itr % args.log_test == 0:
        test_loss, test_acc = test(model, device, test_loader)
        logs['test_loss'].append(test_loss)
        logs['test_acc'].append(test_acc)
        varepsilon = Privacy.ComputePrivacy(Sampled_ratio, args.z, itr, delta, 32)
        print('Privacy Budget: {:.6f}; {} -th Iteration'.format(varepsilon, itr))
        logs['varepsilon'].append(varepsilon)
        with open('../results/REDDIT/MAPA_S_Asyn_04flat_TestLoss.txt', 'a+') as fl:
            fl.write(str(test_loss.item()) + '\t')
        with open('../results/REDDIT/MAPA_S_Asyn_04flat_TestAcc.txt', 'a+') as fl:
            fl.write(str(test_acc.item()) + '\t')
        with open('../results/REDDIT/MAPA_S_Asyn_04flat_Budget.txt', 'a+') as fl:
            fl.write(str(varepsilon) + '\t')

    ########## Adjust the learning rate and number of iterarions for next stage ##########
    if itr >= Stage_Itr_count:
        Grad_Upper = args.redu_ratio * Grad_Upper
        Clip_Bound = ClipBound_gerate(Grad_Upper, Layers_nodes, style=args.ClipStyle)
        Stage_Lr, Stage_Itr_current = Stage_Lr_Itrs(Grad_Upper, Layers_nodes, Leaf_split)
        Stage_Itr_count += Stage_Itr_current
        Stage_count += 1
        print('Stage number: {}, - - -  Stage_Lr = {}, Stage_iterations = {}'.format(Stage_count, Stage_Lr,
                                                                                     Stage_Itr_current))