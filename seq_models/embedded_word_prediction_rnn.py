"""
A simple gru demonstration for discrete sequential prediction using pytorch. This is just for learning pytorch.
RNN model: Given an input symbol and the current hidden state, predict the next character. So we have
discrete one-hot input, and discrete one-hot output.

Sample output:
	
	python3 BPTT.py -batchSize=4 -maxEpochs=2000 -momentum=0.9 -eta=1E-3 -hiddenUnits=25
	...


"""

#serialization. TODO: Remove these and serialization to separate file/class
import json
import base64
import os
import pickle


import torch
import random
import matplotlib.pyplot as plt
from torch_optimizer_builder import OptimizerFactory
import matplotlib.pyplot as plt

TORCH_DTYPE=torch.float32
torch.set_default_dtype(TORCH_DTYPE)

VERBOSE = False

#TODO: Remove this to separate generation component. It is used for beam search based inference.
class Node(object):
	def __init__(self):
		Parent=None
		Index = -1
		LogProb = 1.0

"""
A GRU cell with softmax output off the hidden state; word-embedding input/output, for some word prediction sandboxing.


@useRNN: Using the built-in torch RNN is a simple swap, since it uses the same api as the GRU, so pass this to try an RNN
"""
class EmbeddedGRU(torch.nn.Module):
	def __init__(self, xdim, hdim, ydim, numHiddenLayers, batchFirst=True, clip=-1, useRNN=False):
		super(EmbeddedGRU, self).__init__()
		self._initialize(xdim, hdim, ydim, numHiddenLayers, batchFirst, clip, useRNN)

	def _initialize(self, xdim, hdim, ydim, numHiddenLayers, batchFirst=True, clip=-1, useRNN=False):
		"""
		This is here instead of in the ctor because I want a single point for initialization,
		for both construction and deserialization of an existing model.
		"""
		self._optimizerBuilder = OptimizerFactory()
		self._batchFirst = batchFirst
		self.xdim = xdim
		self.hdim = hdim
		self.ydim = ydim
		self.numHiddenLayers = numHiddenLayers
		#build the network models
		if useRNN:
			self.rnn = torch.nn.RNN(input_size=xdim, hidden_size=hdim, num_layers=numHiddenLayers, batch_first=self._batchFirst)
			self.modelType = "rnn"
		else:
			self.rnn = torch.nn.GRU(input_size=xdim, hidden_size=hdim, num_layers=numHiddenLayers, batch_first=self._batchFirst)
			self.modelType = "gru"

		self.linear = torch.nn.Linear(hdim, ydim)
		#LogSoftmax @dim refers to the dimension along which LogSoftmax (a function, not a layer) will apply softmax.
		# dim=2, since the output of the network is size (batchSize x seqLen x ydim) and we want to calculate softmax at each output, hence dimension 2.
		self.logSoftmax = torch.nn.LogSoftmax(dim=2)
		self._initWeights()
		if clip > 0: #this is used as a flag to determine if clip_grad_norm_ will be called
			self._clip = 1
		else:
			self._clip = -1

		#print("State dict: "+str(self.state_dict()))
		#self.Save()
		self.Read()

	def _initWeights(self, initRange=1.0):
		for gruWeights in self.rnn.all_weights:
			for weight in gruWeights:
				weight.data.uniform_(-initRange, initRange)
		self.linear.weight.data.uniform_(-initRange, initRange)

	def forward(self, x_t, hidden=None, verbose=False):
		"""
		@X_t: Input of size (batchSize x seqLen x xdim).
		@hidden: Hidden states of size (1 x batchSize x hdim), or None, which if passed will initialize hidden states to 0.

		Returns: @output of size (batchSize x seqLen x ydim), @z_t (new hidden state) of size (batchSize x seqLen x hdim), @hidden the final hidden state of dim (1 x @batchSize x hdim)
		NOTE: Note that @batchSize is in different locations of @hidden on input vs output
		"""
		z_t, finalHidden = self.rnn(x_t, hidden) #@output contains all hidden states [1..t], whereas @hidden only contains the final hidden state
		s_t = self.linear(z_t)
		output = self.logSoftmax(s_t)
		if verbose:
			print("x_t size: {} hidden size: {}  z_t size: {} s_t size: {} output.size(): {}".format(x_t.size(), hidden.size(), z_t.size(), s_t.size(), output.size()))

		return output, z_t, finalHidden

	"""
	The axis semantics are (num_layers, minibatch_size, hidden_dim).
	@batchFirst: Determines if batchSize comes before or after numHiddenLayers in tensor dimension
	Returns @batchSize copies of the zero vector as the initial state; hence size (@batchSize x numHiddenLayers x hdim)
	"""
	def initHidden(self, batchSize, numHiddenLayers=1, batchFirst=False, requiresGrad=True):
		if batchFirst:
			hidden = torch.zeros(batchSize, numHiddenLayers, self.hdim, requires_grad=requiresGrad).to(TORCH_DTYPE)
		else:
			hidden = torch.zeros(numHiddenLayers, batchSize, self.hdim, requires_grad=requiresGrad).to(TORCH_DTYPE)
		return hidden

	def initRandHidden(self, batchSize, numHiddenLayers=1, batchFirst=False, scale=1.0, requiresGrad=True):
		"""
		Initializes a random hidden state. This is for tasks like generation, from
		a random initial hidden state.

		@scale: Output of torch.randn contains numbers drawn from a zero mean 1-stdev Gaussian; @scale scales these to
		a different scale.
		"""
		if batchFirst:
			hidden = scale * torch.randn(batchSize, numHiddenLayers, self.hdim, requires_grad=requiresGrad).to(TORCH_DTYPE)
		else:
			hidden = scale * torch.randn(numHiddenLayers, batchSize, self.hdim, requires_grad=requiresGrad).to(TORCH_DTYPE)
		return hidden

	def sampleMaxIndex(self, v, stochasticChoice=False):
		"""
		Given a stochastic vector (1 x n) vector @v, returns the max index of the vector
		under one of two strategies:
			@stochasticChoice = false: just return the index of the max value in the vector (e.g. argmax(v))
			@stochasticChoice = true: return the index sampled from the distribution of the vector. This
			is done by selecting a uniform random number in [0,1.0], then returning the index containing
			this number in its range. If v = [0.3, 0.3, 0.4] and r=0.5, returns 1, since the 0.5 occurs
			in the span of the second entry, [0.3-0.6).
		"""
		maxIndex = 0
		
		if not stochasticChoice:
			#output of logsoftmax are log probabilities, so max prediction is still just the scalar max of output vector
			maxIndex = int(v.argmax(dim=0))
		else:
			#choose maxIndex stochastically according ot the distribution of the output
			p = torch.exp(v)  #get the output distribution as non-log probs
			r = torch.rand(1)[0]
			c = 0.0
			maxIndex = 0
			for i in range(p.size()[0]):
				if c >= r:
					maxIndex = i
					break
				c += p[i]

		return maxIndex

	def beamGenerate(self, vecModel, k=1, beamWidth=1, depth=1, numSeqs=1, seqLen=20):
		"""
		Inference procedure for generating language using a basic bfs beam search: At each node, expand and take its top k children,
		ranked by probability. No fancy value estimation of node value allowed in this method (e.g., SEARN'ing).
		In this algorithm, the beam is reset at each layer of the search tree:
			children = getChildren(beam, k) #expand all children and take their k-max children (the maxes are scoped to each parent node)
			beam = sorted(children)[:beamWidth]

		TODO: DFS and other forms of beam search... can't remember Jana's thesis...
		TODO: Is there a form of Viterbi if the network if bidirectional?
		"""
		print("\n############### Generating {} sequences with beam search k={} beamWidth={} depth={} seqLen={} ###############".format(numSeqs,k,beamWidth,depth,seqLen))

		#x_t = torch.zeros(1, 1, self.xdim, requires_grad=False)

		for _ in range(numSeqs):
			"""
			#reset network
			hidden = self.initHidden(1, self.numHiddenLayers, requiresGrad=False)
			maxIndex = random.randint(0,self.xdim-1)
			lastIndex = maxIndex
			word = vecModel.wv.index2entity[maxIndex]
			x_t[0][0][:] = torch.tensor(vecModel.wv.get_vector(word)[:], requires_grad=False)
			seq = word
			"""
			#initialize random hidden state, then init beam
			hidden = self.initRandHidden(1, self.numHiddenLayers, requiresGrad=False)
			x_t = torch.zeros(1, 1, self.xdim, requires_grad=False)
			o_t, z_t, discarded_hidden = self(x_t, hidden, verbose=False)
			maxIndex = self.sampleMaxIndex(o_t[-1][-1], stochasticChoice=False)
			#just start with the max prob first term, so a beam of one node
			beam = [ Node(Parent=None, Index=maxIndex, LogProb=o_t[-1][-1][maxIndex]) ]

			for _ in range(seqLen):
				#def getChildren(beam, k)
				children = []
				for parent in beam:
					word = vecModel.wv.index2entity[ parent.Index ]
					x_t[0][0][:] = torch.tensor(vecModel.wv.get_vector(word)[:], requires_grad=False)					
					#@o_t output of size (1 x 1 x ydim), @z_t (new hidden state) of size (1 x 1 x hdim)
					#In this special case, @hidden and z_t are the same, since only one-step of prediction has been performed
					o_t, z_t, discarded_hidden = self(x_t, parent.hidden, verbose=False)
					maxIndices = self.sampleMaxIndices(o_t[-1][-1], k)
					#Should hidden state also 
					children += [Node(Parent=node, Index=tup[0], LogProb=tup[1]+parent.LogProb) for tup in maxIndices]
				#reset beam to top @beamWidth candidate nodes
				beam = sorted(children, key=lambda node: node.LogProb, reverse=True)[:beamWidth]

			#backtrack()
			#backtrack from all beam nodes to get full sequences
			sequences = [] # store sequences as tuples: (log-prob, [a,b,c...]) but sequence is reversed
			for node in beam:
				prob = node.LogProb
				sequence = []
				parent = node
				while parent != None:
					w = vecModel.wv.index2entity[ parent.Index ]
					sequence += w
					parent = parent.Parent
				sequences += (prob, [w for w in reverse(sequence)])

			print("Top sequences ranked by log-prob")
			for seq in sequences:
				print("{} {}".format(seq[0], " ".join(seq[1])))

			return sequences

	def sampleMaxIndices(self, v, k):
		"""
		Returns k-max indices of (1 x n) tensor @v, returning these as a list of tuples: (index, log-prob).
		TODO: Optimize system-memory complexity of this function to reduce array creations/copies. Also minimize log-prob calcs;
		can sort on the k-maxes, then apply logs to these once found, instead of log'ing the whole tensor (if it isn't already in log space).
		TODO: numpy() call will cost dearly at this level, since this runs in inner loops.
		TODO: For k <= 3, this could be an inline function with O(3n) complexity: get max 3 times. But in fact,
		getting the top 3 can be reduced to O(1.5n), by inheriting info of the top two comparisons.
		"""
		#TODO: Can't remember if the outputs are already log probs or not...
		return np.argpartition(v.numpy(), k)

	def generate(self, vecModel, numSeqs=1, seqLen=50, stochasticChoice=False, allowRecurrentNoise=False):
		"""
		Generates one word at a time, via a simple max procedure. Ergo, this is bigram generation.
		@reverseEncoding: A gensim word2vec model for converting output probabilities and their indices back into words.
		@numSeqs: Number of sequences to generate
		@seqLen: The length of each generated sequence, before stopping generation
		@stochasticChoice: If True, then next character is sampled according to the softmax distribution
			over output words, as opposed to selecting the maximum probability prediction. Note for large models, this
			likely won't work very well, since the portion of probability assigned to terms is likely very small even
			for the max term.
		@allowRecurrentNoise: Just an interesting parameter to observe: during generation, the output y'
		becomes the input to the next stage, and canonically should be one-hotted such that only the max
		entry is 1.0, and all others zero. However you can instead not one-hot the vector, leaving other noise
		in the vector. No idea what this will do, its just interesting to leave in.
		"""
		print("\n###################### Generating {} sequences with stochasticChoice={} #######################".format(numSeqs,stochasticChoice))

		for _ in range(numSeqs):
			#reset network
			hidden = self.initHidden(1, self.numHiddenLayers, requiresGrad=False)
			x_t = torch.zeros(1, 1, self.xdim, requires_grad=False)
			maxIndex = random.randint(0,self.xdim-1)
			lastIndex = maxIndex
			word = vecModel.wv.index2entity[maxIndex]
			x_t[0][0][:] = torch.tensor(vecModel.wv.get_vector(word)[:], requires_grad=False)
			seq = word
			for _ in range(seqLen):
				#@o_t output of size (1 x 1 x ydim), @z_t (new hidden state) of size (1 x 1 x hdim)
				#In this special case, @hidden and z_t are the same, since only one-step of prediction has been performed
				o_t, z_t, hidden = self(x_t, hidden, verbose=False)
				#self.visualizeOutputs(o_t[0][0], vecModel)
				maxIndex = self.sampleMaxIndex(o_t[-1][-1], stochasticChoice)
				if maxIndex == lastIndex: #resample if a cycle occurs
					o_t[-1][-1][maxIndex] = -100000
					maxIndex = self.sampleMaxIndex(o_t[-1][-1], stochasticChoice)
				lastIndex = maxIndex
				word = vecModel.wv.index2entity[maxIndex]
				seq += (" " + word)
				#TODO: probly a faster way than this to get word vector from word index
				x_t.zero_()
				x_t[0][0][:] = torch.tensor(vecModel.wv.get_vector(word)[:], requires_grad=False)

			print(seq+"<")

	def visualizeOutputs(self, v, vecModel):
		plot = self.plotDistribution(v)
		plt.show()

	def plotDistribution(self, v):
		"""
		An interesting ad hoc way to view the output distribution generated by the trained network is simply to plot the pdf over
		the entire output range, to see if the network's output probabilities are more uniform or more exponential. Natural language
		tends to be more exponential, hence we'd like to see it that way; exponential could also represent overfitting, which is bad.
		But its worth a gander.

		@v: A tensor of size 1xC, where C is the number of output classes, whose entries represent logsoftmax output probabilities over the range of C.
		@vecmodel: Allows getting the top-k most likely classes in the output via their indices in the output vector
		"""
		v = torch.exp(v)
		orderedProbs = sorted([(i,p.item()) for i, p in enumerate(list(v))], key=lambda t: t[1], reverse=True)
		print("Output distribution")
		ys = [t[1] for t in orderedProbs]
		xs = [x+1 for x in range(len(ys))]
		plot = plt.plot(xs,ys)
		return plot

	def train(self, dataset, epochs, torchEta=1E-3, momentum=0.9, optimizerStr="adam", ignoreIndex=-1):
		"""
		This is just a working example of a torch BPTT network; it is far from correct yet.
		The hyperparameters and training regime are not optimized or even verified, other than
		showing they work with the same performance as the rnn implemented in numpy from scratch.

		According to torch docs it might be possible to leave this is in its explicit example/update form,
		but instead simply accumulate the gradient updates over multiple time steps, or multiple sequences,
		by simply choosing when to zero the gradient with rnn.zero_grad().

		A very brief example from the torch docs, for reference wrt dimensions of input, hidden, output:
			>>> rnn = nn.GRU(10, 20, 2)    	  	# <-- |x|, |h|, num-layers
			>>> input = torch.randn(5, 3, 10) 	# <-- 1 batch of 5 training example in sequence of length 3, input dimension 10
			>>> h0 = torch.randn(2, 3, 20)		# <-- 2 hidden states matching sequence length of 3, hidden dimension 20; 2 hidden states, because this GRU has two layers
			>>> output, hn = rnn(input, h0)

		@dataset: A list of lists, where each list represents one training sequence and consists of (x,y) pairs
				  of one-hot encoded vectors.

		@epochs: Number of training epochs. Internally this is calculated as n/@batchSize, where n=|dataset|
		@batchSize: Number of sequences per batch to train over before backpropagating the sum gradients.
		@torchEta: Learning rate
		@bpttStepLimit: the number of timesteps over which to backpropagate before truncating; some papers are
				quite generous with this parameter (steps=30 or so), despite possibility of gradient issues.
		@ignore_index: Output target index values to ignore. These represent missing words or other non-targets. See pytorch docs.
		"""

		#define the negative log-likelihood loss function
		criterion = torch.nn.NLLLoss(ignore_index=ignoreIndex)
		#swap different optimizers per training regime
		optimizer = self._optimizerBuilder.GetOptimizer(parameters=self.parameters(), lr=torchEta, momentum=momentum, optimizer=optimizerStr)

		ct = 0
		k = 20
		losses = []
		nanDetected = False

		#try just allows user to press ctrl+c to interrupt training and observe his or her network at any point
		try:
			for epoch in range(epochs):
				x_batch, y_batch = dataset.getNextBatch()
				#batchSeqLen = x_batch.size()[1]  #the padded length of each training sequence in this batch
				batchSize = x_batch.shape[0]
				hidden = self.initHidden(batchSize, self.numHiddenLayers)
				# Forward pass: Compute predicted y by passing x to the model
				y_hat, _, _ = self(x_batch, hidden, verbose=VERBOSE)
				# Compute and print loss. As a one-hot target nl-loss, the target parameter is a vector of indices representing the index
				# of the target value at each time step t.
				loss = criterion(y_hat.view(-1,self.ydim), y_batch.to(torch.int64).view(-1)) #criterion input is (N,C), where N=batch-size and C=num classes
				nanDetected = nanDetected or torch.isnan(loss)
				losses.append(loss.item())
				if epoch % 50 == 49: #print loss eveyr 50 epochs
					avgLoss = sum(losses[epoch-k:]) / float(k)
					print(epoch, avgLoss)
					if nanDetected:
						print("Nan loss detected; suggest mitigating with shorter training regimes (shorter sequences) or gradient clipping")	
				#print(loss)
				# Zero gradients, perform a backward pass, and update the weights.
				optimizer.zero_grad()
				loss.backward()
				#note this is the wrong way to clip gradients, which should be done during backprop, not after backprop has accumulated all gradients, but torch doesn't support this easily
				if self._clip > 0.0:
				 	torch.nn.utils.clip_grad_norm_(self.parameters(), self._clip)

				optimizer.step()

				#TODO: Kludgy move
				if epoch > 4000 and epoch < 4500:
					optimizer = self._optimizerBuilder.GetOptimizer(parameters=self.parameters(), lr=torchEta*0.1, momentum=momentum, optimizer=optimizerStr)
	
		except (KeyboardInterrupt):
			self.Save()

		#plot the losses
		k = 20
		avgLosses = [sum(losses[i:i+k])/float(k) for i in range(len(losses)-k)]
		xs = [i for i in range(len(avgLosses))]
		plt.plot(xs,avgLosses)
		plt.show()

	################### Serialization. This could be removed to its own class if desired ###########################
	def Save(self):
		modelFolder = "./rnn_models/"
		if input("Save model? (Enter y/n) ").lower() in ["y","yes"]:
			path = input("Enter model name for "+modelFolder+" folder: ")
			self._save(modelFolder+path)

	def Read(self):
		if input("Read existing model? (Enter y/n) ").lower() in ["n","no"]:
			return

		modelDir = "./rnn_models/"
		models = [(str(i), modelDir+model) for i, model in enumerate(os.listdir(modelDir)) if ".json" in model]
		if len(models) == 0:
			print("No models in "+modelDir)
			return
		for i, model in models:
			print("\t{}: {}".format(i, model))
		done = False
		while not done:
			modelNum = input("Enter number of model to select: ").lower()
			done = modelNum in [i for i, model in models]
			if not done:
				print("Re-enter")
		self._read(models[ int(modelNum) ][1] )

	#WARNING/TODO: These two invertible functions are very lazy/unsafe, for local serialization only (e.g., no guarantees it can be transferred or loaded on another machine/architecture)
	def _serializeObject(self, obj):
		pickled = pickle.dumps(obj)
		return base64.b64encode(pickled).decode("utf-8")
	def _deserializeObject(self, s):
		pickled = base64.b64decode(s)
		return pickle.loads(pickled)

	def _read(self, ipath):
		with open(ipath, "r") as ifile:
			asDict = json.load(ifile)
			self._fromDict(asDict)

	def _save(self, opath):
		"""
		Saves entire GRU/RNN object to a json file with pickled+base64 encoded torch components
		"""
		if not opath.endswith(".json"):
			opath += ".json"
		print("Saving model to {} ...".format(opath))
		with open(opath, "w+") as ofile:
			asDict = self._toDict()
			#print("DICT: \n"+str(asDict))
			asJson = json.dumps(asDict, sort_keys=True, indent=4)
			#print("\n\nJSON: "+asJson)
			ofile.write(asJson+"\n")
			print("Model saved as json to "+opath)

	def _fromDict(self, d):
		self._batchFirst = d["batchFirst"]
		self.xdim = d["xdim"]
		self.hdim = d["hdim"]
		self.ydim = d["ydim"]
		self.numHiddenLayers = d["numHiddenLayers"]
		self.modelType = d["modelType"]
		self._clip = d["clip"]
		self.rnn = self._deserializeObject(d["rnn"])
		self.linear = self._deserializeObject(d["linear"])
		self.logSoftmax = torch.nn.LogSoftmax(dim=2)
		if d["torchDtype"] != str(TORCH_DTYPE):
			print("WARNING: read dict with torchDtype={} but local dtype is {}".format(d["torchDtype"],str(TORCH_DTYPE)))

	def _toDict(self):
		#Returns ascii-serializable dict
		return {														\
			"batchFirst": self._batchFirst, 							\
			"xdim" : self.xdim, 										\
			"hdim" : self.hdim, 										\
			"ydim" : self.ydim, 										\
			"numHiddenLayers" : self.numHiddenLayers,					\
			"modelType" : self.modelType, 								\
			"clip" : self._clip, 										\
			"rnn":    self._serializeObject(self.rnn),		\
			"linear": self._serializeObject(self.linear),	\
			"torchDtype": str(TORCH_DTYPE)								\
		}

