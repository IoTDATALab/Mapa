import os
from params import *
import _pickle as cPickle
import paho.mqtt.client as mqtt
import queue
import math
import ComputePrivacy as Privacy

import logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

DELAY = int(os.getenv('DELAY'))
MQTT_PORT = int(os.environ.get('MQTT_PORT'))
MQTT_IP = os.environ.get('MQTT_IP')
TEST_NUM = int(os.environ.get('TEST_NUM'))
RESULT_ROOT = os.environ.get('RESULT_ROOT')


L = 0.0001
z = 0.8
init_bound = 1.0
Grad_upper = 1.0
m = 72377
grad_var = 2.0
redu_ratio = 0.8
T = 1000
tau = 0
BATCH_SIZE = 5
msgQueue = queue.Queue()
def Init_Gradient(params):
    grads = []
    for i in range(len(params)):
        grads.append(torch.zeros(params[i].shape))
    return grads

def on_connect(mqttc, obj, flags, rc):
    print("rc: " + str(rc))


def on_message(mqttc, obj, msg):
    print("received: " + msg.topic + " " + str(msg.qos))
    msgQueue.put(msg.payload)

client = mqtt.Client()
client.on_connect = on_connect
client.on_message = on_message
#client.enable_logger(logger)
client.connect(MQTT_IP, MQTT_PORT, 600)
client.subscribe("mapa_grads/#", 2)
client.loop_start()

if __name__ == '__main__':

    params = InitializeParameters()
    payload = [Grad_upper, params]
    client.publish("init", cPickle.dumps(payload), 2)

    Layers_node = []
    for i in range(len(params)):
        Layers_node.append(params[i].numel())

    Stage_count = 1
    total_step = 0
    epsilon = []
    Sampled_ratio = BATCH_SIZE / m
    delta = 1. / m ** 1.1
    for t in range(T):

        delta_b = (grad_var ** 2 + sum(Layers_node) * (z * Grad_upper) ** 2) / BATCH_SIZE
        P = 2 * delta_b / (redu_ratio ** 2 * Grad_upper ** 2 * (tau + 1))
        LR = 1 / (2 * max(P, 1) * L * (tau + 1))
        # print("LR: ",LR)
        T0 = math.ceil(4 * P ** 2 * L * (tau + 1) ** 2 * init_bound / delta_b)
        #print(T0)

        for ts in range(T0):

            grads_sum = Init_Gradient(params)
            for i in range(DELAY):
                grads = cPickle.loads(msgQueue.get())
                for i in range(len(grads)):
                    grads_sum[i] += grads[i]

            # avg grads
            for i in range(len(grads_sum)):
                grads_sum[i] /= DELAY

            for i in range(len(params)):
                params[i] = params[i].float()
                params[i] -= LR * grads_sum[i]

            payload = [Grad_upper, params]
            client.publish("mapa_params", cPickle.dumps(payload), 2)

            if total_step % TEST_NUM == 0:
                man_file = open(RESULT_ROOT + '[MAPA_Budget]', 'w')
                varepsilon = Privacy.ComputePrivacy(Sampled_ratio, z, total_step + 1, delta, 32)
                epsilon.append(varepsilon)
                print(epsilon, file=man_file)
                man_file.close()
            total_step += 1
            #print('Stage number: {}, Stage_Lr = {}, Stage_iterations = {}'.format(Stage_count, LR, ts + 1))

            if total_step == T:
                break

        Grad_upper = redu_ratio * Grad_upper
        Stage_count += 1

        if total_step == T:
            print(T)
            break


