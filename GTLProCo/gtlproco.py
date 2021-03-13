from .LabelledGraph import LabelledGraph
from .GTLformula import *

# Import the optimizer tools
import gurobipy as gp
import time

def create_linearize_constr(mOpt, xDen, MarkovM, dictSlack, dictConstr, swam_ids, Kp, node_ids):
	""" Create a gurobi prototype of linearized nonconvex constraint
		around the solution of the previous iteration.
		Additionally, create the slack variables for the constraints.
	"""
	# Add the slack variable 
	for s_id in swam_ids:
		for t in range(Kp):
			for n_id in node_ids:
				dictSlack[(s_id, n_id, t)] = \
					mOpt.addVar(lb=-gp.GRB.INFINITY, name='Z[{}][{}]({})'.format(s_id,n_id,t))

	# Add the absolute value of slack variable
	for s_id in swam_ids:
		for t in range(Kp):
			for n_id in node_ids:
				dictSlack[(s_id, -n_id-1, t)] = \
					mOpt.addVar(lb=0, name='AZ[{}][{}]({})'.format(s_id,n_id,t))

	# Create the linearized constraints
	for s_id in swam_ids:
		for t in range(Kp):
			for n_id in node_ids:
				dictConstr[(s_id, n_id, t)] = mOpt.addLConstr(
					gp.LinExpr([1,-1]+ [0 for i in node_ids] + [0 for i in node_ids], 
						[xDen[(s_id,n_id,t+1)], dictSlack[(s_id, n_id, t)]] + \
						[ xDen[(s_id,m_id,t)] for m_id in node_ids] + \
						[ MarkovM[(s_id,n_id,m_id,t)] for m_id in node_ids]
					), 
					gp.GRB.EQUAL, 
					0, 
					name='Lin[{}][{}]({})'.format(s_id,n_id,t))

	# Create the constraints by the absolute value of the slack variable
	for s_id in swam_ids:
		for t in range(Kp):
			for n_id in node_ids:
				mOpt.addConstr(dictSlack[(s_id, n_id,t)] <= dictSlack[(s_id, -n_id-1,t)])
				mOpt.addConstr(dictSlack[(s_id, n_id,t)] >= -dictSlack[(s_id, -n_id-1,t)])

def update_opt_step_k(mOpt, xDen, MarkovM, initRange, dictConstr, trustRegion, xk, Mk, swam_ids, Kp, node_ids):
	"""
		Update the optimization problem based on the optimal solution at the past iteration
		given by xk and Mk
	"""
	# Set the constraint limits on M imposed by the trust region
	for s_id in swarm_ids:
		for n_id in node_ids:
			for m_id in node_ids:
				for t in range(Kp):
					MarkovM[(s_id, n_id, m_id, t)].lb = \
						np.maximum(Mk[(s_id, n_id, m_id, t)]-trustRegion, initRange[(s_id, n_id, m_id, t)][0])
					MarkovM[(s_id, n_id, m_id, t)].ub = \
						np.minimum(Mk[(s_id, n_id, m_id, t)]+trustRegion, initRange[(s_id, n_id, m_id, t)][1])

	for (s_id, n_id, t), c in dictConstr.items():
		c.RHS = -sum([Mk[(s_id, n_id, m_id, t)]*xk[(s_id, m_id, t)] for m_id in node_ids])
		for m_id in node_ids:
			mOpt.chgCoeff(c, MarkovM[(s_id, n_id, m_id, t)], -xk[(s_id, m_id, t)])
			mOpt.chgCoeff(c, xDen[(s_id, m_id, t)], - Mk[(s_id, n_id, m_id, t)])


def compute_actual_penalized_cost(cost_fun, lam, xk, Mk, swam_ids, Kp, node_ids):
	""" Compute the penalized cost function based on the constraint x(t+1) = M(t) x(t)
		In the paper, this penalized cost is referred to as J
	"""
	costVal = get_cost_function(cost_fun, xk, Mk, swam_ids, Kp, node_ids)
	penList = list()
	for s_id in swam_ids:
		for t in range(Kp):
			for n_id in node_ids:
				penList.append(np.abs(xk[(s_id,n_id,t+1)] \
								- sum([ Mk[(s_id,n_id,m_id,t)]* xk[(s_id, m_id, t)] \
										for m_id in node_ids])))
	return costVal + lam * sum(penList)


def sequential_optimization(mOpt, cost_fun, xDen, MarkovM, dictConstr, swam_ids, Kp, 
			node_ids, maxIter=20, epsTol=1e-5, lamda=10, devM=0.5, rho0=1e-5, 
			rho1=0.5, rho2=0.8, alpha=2.0, beta=1.5, 
			timeLimit=5, n_thread=0, verbose=True, autotune=True):
	""" Solve the sequential mixed integer linear program
	:param maxIter : The maximum number of iteration of the algorithm
	:param epsTol : The desired tolerance of the optimal solution
	:param lamda : Penalty cost for violating the state evolution constraint
	:param devM : Initial deviation between two consecutive iterates in control
	:param rho0 : rho0 < rho1 <<1, Threshold to consider the aproximation too inaccurate -> shrink
	:param rho1 : rho1 < rho2, Threshold to consider the approximation good (so accept current step) but need to shrink the trust region
	:param rho2 : 0 << rho2 < 1, Threshold to consider the approx good and increment the trust region
	:param alpha : alpha > 1, factor used to shrink the trust region
	:param beta : beta > 1, factor used to inflate the trust region
	"""
	dictXk = dict()
	dictMk = dict()
	initRange = dict()
	# Save the initial point that do not need to be feasible
	for s_id in swam_ids:
		for m_id in node_ids:
			for t in range(Kp+1):
				dictXk[(s_id, m_id, t)] = 0.5 *(xDen[(s_id, m_id, t)].lb + xDen[(s_id, m_id, t)].ub)
				if t == Kp:
					continue
				countEnforced = 0
				for n_id in node_ids:
					countEnforced += MarkovM[(s_id, n_id,m_id,t)].lb
					dictMk[(s_id, n_id,m_id,t)] = MarkovM[(s_id, n_id,m_id,t)].lb
					# dictMk[(s_id, n_id,m_id,t)] = 0.5*(MarkovM[(s_id, n_id,m_id,t)].lb + MarkovM[(s_id, n_id,m_id,t)].ub)
					initRange[((s_id, n_id,m_id,t))] = (MarkovM[(s_id, n_id,m_id,t)].lb, MarkovM[(s_id, n_id,m_id,t)].ub)
				assert countEnforced < 1
				residueV = 1 - countEnforced
				for n_id in node_ids:
					if dictMk[(s_id, n_id,m_id,t)] + residueV >= MarkovM[(s_id, n_id,m_id,t)].lb and \
						dictMk[(s_id, n_id,m_id,t)] + residueV <= MarkovM[(s_id, n_id,m_id,t)].ub:
						dictMk[(s_id, n_id,m_id,t)] = dictMk[(s_id, n_id,m_id,t)]  + residueV
						break

	# INitialize the trust region
	rk = devM
	Jk = None
	solve_time = 0
	status = -1
	# Set the limit on the time limit
	mOpt.Params.OutputFlag = verbose
	mOpt.Params.Threads = n_thread
	mOpt.Params.TimeLimit = timeLimit
	for iterV in range(maxIter):
		# Check if teh time limit is elapsed
		if solve_time >= timeLimit:
			status = -1
			break
		# Update the optimization problem and solve it
		update_opt_step_k(mOpt, xDen, MarkovM, initRange, dictConstr, rk, dictXk, dictMk, swam_ids, Kp, node_ids)
		if iterV == 0 and autotune:
			mOpt.update()
			mOpt.tune()
		cur_t = time.time()
		mOpt.optimize()
		solve_time += time.time() - cur_t
		if mOpt.status == gp.GRB.OPTIMAL:
			status = 1
		elif mOpt.status == gp.GRB.TIME_LIMIT:
			status = -1
			break
		else:
			status = 0
			break
		dictXk1 = dict()
		dictMk1 = dict()
		for s_id in swam_ids:
			for n_id in node_ids:
				for t in range(Kp+1):
					dictXk1[(s_id, n_id, t)] = xDen[(s_id,n_id,t)].x
					if t == Kp:
						continue
					for m_id in node_ids:
						dictMk1[(s_id, n_id,m_id,t)] = MarkovM[(s_id,n_id,m_id,t)].x
		# Get the optimal cost
		Lk1 = mOpt.objVal
		if Jk is None:
			Jk = compute_actual_penalized_cost(cost_fun, lamda, dictXk, dictMk, swam_ids, Kp, node_ids)
		Jk1 = compute_actual_penalized_cost(cost_fun, lamda, dictXk1, dictMk1, swam_ids, Kp, node_ids)
		deltaJk = Jk - Jk1
		deltaLk = Jk - Lk1

		# If the solution is good enough
		if np.abs(deltaJk) < epsTol:
			dictXk = dictXk1
			dictMk = dictMk1
			break


		# If the approximation is too loose - contract the trust region and restart the otpimization
		rhok = deltaJk / deltaLk
		if verbose:
			print('Iteration : {}, Rhok : {}, rk : {}'.format(iterV, rhok, rk))
			print('Lk : {}, Lk+1 : {}'.format(Jk, Lk1))
			print('Jk : {}, Jk+1 : {}'.format(Jk, Jk1))
			print('deltaJk : ', deltaJk)
			print('deltaLk : ', deltaLk)
			# print('Jk+1 : ', Jk1)
		if rhok < rho0:
			rk = rk / alpha
			continue

		# Accept this step
		dictXk = dictXk1
		dictMk = dictMk1
		Jk = Jk1

		# UPdate the trust region if needed
		rk = rk / alpha if rhok < rho1 else (beta*rk if rho2 < rhok else rk)
		rk = np.minimum(rk, 1) # rk should be less than 1 due to the Markov matrix values
		if rk < 1e-6:
			print(' Algorithm stopped : {}<1e-6, trust region too small'.format(rk))
			break
	# return dictXk, dictMk1
	return status, solve_time


def create_den_constr(xDen, swam_ids, Kp, node_ids):
	""" Given symbolic variables (gurobi, pyomo, etc..) representing the densities
		distribution at all times and all nodes, this function returns the 
		constraint 1^T x^s(t) = 1 for all s and t
	"""
	listConstr = list()
	for s_id in swam_ids:
		for t in range(Kp+1):
			listConstr.append(sum([ xDen[(s_id, n_id, t)] for n_id in node_ids]) == 1)
	return listConstr

def create_markov_constr(MarkovM, swam_ids, Kp, node_ids):
	""" Given symbolic variables (gurobi, pyomo, etc..) representing the Markov matrices
		at all times, this function returns the constraint 1^T M^s(t) = 1 for all s and t
		and M_ij^s(t) == 0 for (j,i) not in the edge list
	"""
	listConstr = list()
	# Stochastic natrix constraint
	for s_id in swam_ids:
		for t in range(Kp):
			for n_id in node_ids:
				listConstr.append(sum([MarkovM[(s_id,m_id,n_id,t)] for m_id in node_ids]) == 1)
	# Adjacency natrix constraints
	# for s_id, edges in edgeSwarm.items():
	# 	for t in range(Kp):
	# 		for (n_id, m_id) in edges:
	# 			listConstr.append(MarkovM[(s_id, m_id, n_id, t)] == 0)

	return listConstr

def create_bilinear_constr(xDen, MarkovM, swam_ids, Kp, node_ids):
	""" Return the bilinear constraint M(t) x(t) = x(t+1)
	"""
	listConstr = list()
	for s_id in swam_ids:
		for t in range(Kp):
			for n_id in node_ids:
				listConstr.append(sum([ MarkovM[(s_id,n_id,m_id,t)]* xDen[(s_id, m_id, t)] for m_id in node_ids])\
									== xDen[(s_id, n_id, t+1)])
	return listConstr


def get_cost_function(cost_fun, xDen, MarkovM, swam_ids, Kp, node_ids):
	""" Return the cost function over the graph trajectory length
	"""
	costVal = 0
	if cost_fun is not None:
		for t in range(Kp):
			xDict = dict()
			MDict = dict()
			for s_id in swam_ids:
				for n_id in node_ids:
					xDict[(s_id, n_id)] = xDen[(s_id, n_id, t)]
					for m_id in node_ids:
						MDict[(s_id, n_id, m_id)] = MarkovM[(s_id, n_id, m_id, t)]
			costVal += cost_fun(xDict, MDict, swam_ids, node_ids)
	return costVal


def create_minlp_model(util_funs, milp_expr, lG, Kp, cost_fun=None, addBilinear=True, solve=False):
	# Obtain the milp encoding
	(newCoeffs, newVars, rhsVals, nVar, timeVal), (lCoeffs, lVars, lRHS) = milp_expr

	# Get the boolean and continuous variables from the MILP expressions of the GTL
	bVars, rVars, lVars, denVars = getVars(newVars, timeVal, lVars)

	# Swarm and graph configuration information
	swarm_ids = lG.eSubswarm.keys()
	node_ids = lG.V.copy()

	# Create the different density variables
	xDen = dict()
	for s_id in swarm_ids:
		for n_id in node_ids:
			for t in range(Kp+1):
				xDen[(s_id, n_id, t)] = util_funs['r']('x[{}][{}]({})'.format(s_id, n_id, t))
	# mOpt.addVar(lb=0, ub=1, name='x[{}][{}]({})'.format(s_id, n_id, t))
	
	# Create the different Markov matrices
	MarkovM = dict()
	for s_id in swarm_ids:
		nedges = lG.getSubswarmNonEdgeSet(s_id)
		for n_id in node_ids:
			for m_id in node_ids:
				for t in range(Kp):
					if (m_id, n_id) in nedges:
						MarkovM[(s_id, n_id, m_id, t)] = \
							util_funs['r']('M[{}][{}][{}]({})'.format(s_id, n_id, m_id, t),0,0)
					else:
						MarkovM[(s_id, n_id, m_id, t)] = \
							util_funs['r']('M[{}][{}][{}]({})'.format(s_id, n_id, m_id, t))
	# mOpt.addVar(lb=0, ub=1, name='M[{}][{}][{}]({})'.format(s_id, n_id, m_id, t))
	
	# Create the boolean variables from the GTL formula -> add them to the xDen dictionary
	for (ind0, tInd) in bVars:
		xDen[(ind0, tInd)] = util_funs['b']('b[{}]({})'.format(ind0, tInd))
		# mOpt.addVar(vtype=gp.GRB.BINARY, name='b[{}]({})'.format(ind0, tInd))

	# Create the tempory real variables from the GTL formula -> add them to the xDen dictionary
	for (ind0, tInd) in rVars:
		xDen[(ind0, tInd)] = util_funs['r']('r[{}]({})'.format(ind0, tInd))
		# mOpt.addVar(lb=0, ub=1.0, name='r[{}]({})'.format(ind0, tInd))

	# Create the binary variables from the loop constraint
	for ind0 in lVars:
		xDen[ind0] = util_funs['b']('l[{}]'.format(ind0))
		# mOpt.addVar(vtype=gp.GRB.BINARY, name='l[{}]'.format(ind0))

	# Add the constraint on the density distribution
	listContr = create_den_constr(xDen,swarm_ids, Kp, node_ids)

	# Add the constraints on the Markov matrices
	listContr.extend(create_markov_constr(MarkovM, swarm_ids, Kp, node_ids))

	# Add the bilinear constraints
	if addBilinear:
		listContr.extend(create_bilinear_constr(xDen, MarkovM, swarm_ids, Kp, node_ids))

	# Add the mixed-integer constraints
	listContr.extend(create_gtl_constr(xDen, milp_expr))

	# Add all the constraints
	for constr in listContr:
		util_funs['constr'](constr)

	costVal = get_cost_function(cost_fun, xDen, MarkovM, swarm_ids, Kp, node_ids)
	# set_cost(cVal, xDen, mD, nids, sids)
	util_funs['cost'](costVal, xDen, MarkovM, swarm_ids, node_ids)

	# Solve the problem
	optCost, status, solveTime = -np.inf, -1, 0
	if solve:
		optCost, status, solveTime = util_funs['solve']()

	# Get the solution of the problem
	xDictRes = dict()
	MDictRes = dict()
	ljRes = dict()
	if solve and status ==1:
		for s_id in swarm_ids:
			for n_id in node_ids:
				for t in range(Kp+1):
					xDictRes[(s_id, n_id, t)] = \
						util_funs['opt'](xDen[(s_id, n_id, t)])
		# mOpt.addVar(lb=0, ub=1, name='x[{}][{}]({})'.format(s_id, n_id, t))
		
		for s_id in swarm_ids:
			for n_id in node_ids:
				for m_id in node_ids:
					for t in range(Kp):
						MDictRes[(s_id, n_id, m_id, t)] = \
								util_funs['opt'](MarkovM[(s_id, n_id, m_id, t)])

		for ind0 in lVars:
			ljRes[ind0] = util_funs['opt'](xDen[ind0])
	return optCost, status, solveTime, xDictRes, MDictRes, ljRes, swarm_ids, node_ids


def create_minlp_gurobi(milp_expr, lG, Kp, cost_fun=None, solve=True, 
						timeLimit=5, n_thread=0, verbose=False):
	# Create the Gurobi model
	mOpt = gp.Model('Bilinear MINLP formulation through GUROBI')
	mOpt.Params.OutputFlag = verbose
	mOpt.Params.NonConvex = 2
	mOpt.Params.Threads = n_thread
	mOpt.Params.TimeLimit = timeLimit
	def set_cost(cVal, xDen, mD, sids, nids):
		mOpt.setObjective(cVal, gp.GRB.MINIMIZE)
	def solve_f():
		c_time = time.time()
		mOpt.optimize()
		dur = time.time() - c_time
		if mOpt.status == gp.GRB.OPTIMAL:
			return mOpt.objVal, 1, dur
		elif mOpt.status == gp.GRB.TIME_LIMIT:
			return mOpt.objVal, -1, dur
		else:
			return -np.inf, 0, dur
	def ret_sol(solV):
		return solV.x

	util_funs = dict()
	util_funs['r'] = lambda name, lb=0, ub=1 : mOpt.addVar(lb=lb, ub=ub, name=name)
	util_funs['b'] = lambda name : mOpt.addVar(vtype=gp.GRB.BINARY, name=name)
	util_funs['constr'] = lambda constr : mOpt.addConstr(constr)
	util_funs['solve'] = solve_f
	util_funs['cost'] = set_cost
	util_funs['opt'] = ret_sol
	return create_minlp_model(util_funs, milp_expr, lG, Kp, addBilinear=True, cost_fun=cost_fun, solve=solve)
	# mOpt.update()
	# mOpt.display()

def create_gtl_proco(milp_expr, lG, Kp, cost_fun=None, solve=True, 
					maxIter=20, epsTol=1e-5, lamda=10, devM=0.5, rho0=1e-5, 
					rho1=0.25, rho2=0.7, alpha=2.0, beta=3.5,
					timeLimit=5, n_thread=0, verbose=True, autotune=True):
	# Create the Gurobi model,
	mOpt = gp.Model('Sequential MILP solving through GUROBI')
	# mOpt.Params.OutputFlag = True

	# Actual cost 
	costVal = None
	dictConstr = dict()
	dictSlack = dict()
	xD = None
	MarM = None
	n_ids, s_ids = None, None
	def set_cost(cVal, xDen, mD, sids, nids):
		nonlocal costVal, dictSlack, dictConstr, xD, MarM, n_ids, s_ids
		n_ids = nids
		s_ids = sids
		costVal = cVal
		xD = xDen
		MarM = mD
		# Create the linearized constraints
		create_linearize_constr(mOpt, xD, MarM, dictSlack, dictConstr, s_ids, Kp, n_ids)
		# Add the penalized cost functions
		penList = list()
		for s_id in s_ids:
			for t in range(Kp):
				for n_id in n_ids:
					penList.append(dictSlack[(s_id, -n_id-1,t)])
		pen_cost = costVal + lamda * sum(penList)
		mOpt.setObjective(pen_cost, gp.GRB.MINIMIZE)
		mOpt.update()
		# mOpt.display()

	def solve_f():
		status, dur = sequential_optimization(mOpt, cost_fun, xD, MarM, dictConstr, s_ids, Kp, n_ids,
						maxIter=maxIter, epsTol=epsTol, lamda=lamda, devM=devM, rho0=rho0, 
						rho1=rho1, rho2=rho2, alpha=alpha, beta=beta,
						timeLimit=timeLimit, n_thread=n_thread, verbose=verbose, autotune=autotune)
		if status == 0:
			return -np.inf, status, dur
		return costVal.getValue() if not (isinstance(costVal, int) or isinstance(costVal, float)) else costVal, status, dur

	def ret_sol(solV):
		return solV.x

	util_funs = dict()
	util_funs['r'] = lambda name, lb=0, ub=1 : mOpt.addVar(lb=lb, ub=ub, name=name)
	util_funs['b'] = lambda name : mOpt.addVar(vtype=gp.GRB.BINARY, name=name)
	util_funs['constr'] = lambda constr : mOpt.addConstr(constr)
	util_funs['solve'] = solve_f
	util_funs['cost'] = set_cost
	util_funs['opt'] = ret_sol
	return create_minlp_model(util_funs, milp_expr, lG, Kp, addBilinear=False, cost_fun=cost_fun, solve=solve)
	# mOpt.update()
	# mOpt.display()

def create_minlp_scip(milp_expr, lG, Kp, cost_fun=None, solve=True,
						timeLimit=5, n_thread=0, verbose=False):
	import pyscipopt as pSCIP
	mOpt = pSCIP.Model('Bilinear MINLP formulation through SCIP')
	mOpt.hideOutput(quiet = (not verbose))
	mOpt.setParam('limits/time', timeLimit)
	# mOpt.setParam('')
	def set_cost(cVal, xDen, mD, sids, nids):
		mOpt.setObjective(cVal, "minimize")
	def solve_f():
		c_time = time.time()
		mOpt.optimize()
		dur = time.time() - c_time
		if mOpt.getStatus() == 'optimal':
			return mOpt.getObjVal(), 1, dur
		elif mOpt.getStatus() == 'timelimit':
			return mOpt.getObjVal(), -1, dur
		else:
			return -np.inf, 0, dur
	def ret_sol(solV):
		return mOpt.getVal(solV)
	util_funs = dict()
	util_funs['r'] = lambda name, lb=0, ub=1 : mOpt.addVar(lb=lb, ub=ub, name=name)
	util_funs['b'] = lambda name : mOpt.addVar(vtype="B", name=name)
	util_funs['constr'] = lambda constr : mOpt.addCons(constr)
	util_funs['solve'] = solve_f
	util_funs['cost'] = set_cost
	util_funs['opt'] = ret_sol
	return create_minlp_model(util_funs, milp_expr, lG, Kp, addBilinear=True, cost_fun=cost_fun, solve=solve)
	# mOpt.writeProblem('model')

def create_minlp_pyomo(milp_expr, lG, Kp, cost_fun=None, solve=True, 
			solver='couenne', solverPath='/home/fdjeumou/Documents/non_convex_solver/',
			timeLimit=5, n_thread=0, verbose=False):
	from pyomo.environ import Var, ConcreteModel, Constraint, NonNegativeReals, Binary, SolverFactory
	from pyomo.environ import Objective, minimize
	import pyutilib
	from pyomo.opt import SolverStatus, TerminationCondition

	mOpt = ConcreteModel('Bilinear MINLP formulation through PYOMO')

	# Function to return real values
	def r_values(name, lb=0, ub=1):
		setattr(mOpt, name, Var(bounds=(lb,ub), within=NonNegativeReals))
		return getattr(mOpt, name)

	def b_values(name):
		setattr(mOpt, name, Var(within=Binary))
		return getattr(mOpt, name)

	nbConstr = 0
	def constr(val):
		nonlocal nbConstr
		setattr(mOpt, 'C_{}'.format(nbConstr), Constraint(expr = val))
		nbConstr+=1

	def set_cost(cVal, xDen, mD, sids, nids):
		setattr(mOpt, 'objective', Objective(expr=cVal, sense=minimize))

	def solve_f():
		solverOpt = SolverFactory(solver, executable=solverPath+solver)
		try:
			c_time = time.time()
			results = solverOpt.solve(mOpt, tee=verbose, timelimit=timeLimit)
			dur = time.time() - c_time
			if results.solver.termination_condition == TerminationCondition.optimal:
				return mOpt.objective(), 1, dur
			else:
				return -np.inf, 0, dur
		except pyutilib.common.ApplicationError:
			return -np.inf, -1, timeLimit

	def ret_sol(solV):
		return solV.value

	# mOpt.hideOutput()
	util_funs = dict()
	util_funs['r'] = r_values
	util_funs['b'] = b_values
	util_funs['constr'] = constr
	util_funs['solve'] = solve_f
	util_funs['cost'] = set_cost
	util_funs['opt'] = ret_sol
	return create_minlp_model(util_funs, milp_expr, lG, Kp, addBilinear=True, cost_fun=cost_fun, solve=solve)
	# mOpt.pprint()


if __name__ == "__main__":
    """
    Example of utilization of this class
    """
    V = set({1, 2, 3})
    lG = LabelledGraph(V)

    # Add the subswarm with id 1
    lG.addSubswarm(0, [(1,2), (2,3), (2,1), (3,2)])

    # Add the subswarm with id 1
    lG.addSubswarm(1, [(1,2), (2,3), (2,1), (3,2)])

    # Get the density symbolic representation for defining the node labels
    x = lG.getRprDensity()

    # Now add some node label on the graph
    lG.addNodeLabel(1, x[(0,1)] + x[(0,2)])
    lG.addNodeLabel(2, x[(0,1)] + x[(1,1)])
    lG.addNodeLabel(3, [x[(0,1)] + x[(0,2)], x[(0,3)] - x[(1,2)]])

    Kp = 15
    piV3 = AtomicGTL([0.5, 0.25])
    piV1 = AtomicGTL([0.5])
    formula1 = AlwaysEventuallyGTL(piV3)
    formula2 = AlwaysGTL(piV1)
    m_milp = create_milp_constraints([formula1, formula2], [3, 1], Kp, lG, initTime=0)

    def cost_fun(xDict, MDict, swarm_ids, node_ids):
    	costValue = 0
    	for s_id in swarm_ids:
    		for n_id in node_ids:
    			for m_id in node_ids:
    				costValue += MDict[(s_id, n_id, m_id)]
    	return costValue
    cost_fun = None
    # print_milp_repr([piV3], [3], Kp, lG, initTime=0)

    optCost, status, solveTime, xDictRes, MDictRes, ljRes, swarm_ids, node_ids = \
    						create_minlp_gurobi(m_milp, lG, Kp, cost_fun=cost_fun,
    						timeLimit=5, n_thread=1, verbose=False)
    print(ljRes)
    print(optCost, status, solveTime)
    maxDiff = 0
    for s_id in swarm_ids:
    	for n_id in node_ids:
    		for t in range(Kp):
    			s = sum(MDictRes[(s_id, n_id, m_id, t)]*xDictRes[(s_id, m_id,t)] for m_id in node_ids)
    			# print(np.abs(xDictRes[(s_id, n_id,t+1)]-s))
    			maxDiff = np.maximum(maxDiff, np.abs(xDictRes[(s_id, n_id,t+1)]-s))
    print(maxDiff)

    # create_minlp_scip(m_milp, lG, Kp)
    optCost, status, solveTime, xDictRes, MDictRes, ljRes, swarm_ids, node_ids = \
    								create_minlp_scip(m_milp, lG, Kp, cost_fun=cost_fun,
    									timeLimit=5, n_thread=0, verbose=False)
    print(ljRes)
    print(optCost, status, solveTime)
    maxDiff = 0
    for s_id in swarm_ids:
    	for n_id in node_ids:
    		for t in range(Kp):
    			s = sum(MDictRes[(s_id, n_id, m_id, t)]*xDictRes[(s_id, m_id,t)] for m_id in node_ids)
    			# print(np.abs(xDictRes[(s_id, n_id,t+1)]-s))
    			maxDiff = np.maximum(maxDiff, np.abs(xDictRes[(s_id, n_id,t+1)]-s))
    print(maxDiff)
    # print(xDictRes)

    # create_minlp_pyomo(m_milp, lG, Kp)
    optCost, status, solveTime, xDictRes, MDictRes, ljRes, swarm_ids, node_ids = \
    								create_minlp_pyomo(m_milp, lG, Kp,cost_fun=cost_fun,
    								timeLimit=5, n_thread=0, verbose=False)
    print(ljRes)
    print(optCost, status, solveTime)
    # print(xDictRes)
    # print(optCost)

    optCost, status, solveTime, xDictRes, MDictRes, ljRes, swarm_ids, node_ids = \
    		create_gtl_proco(m_milp, lG, Kp, cost_fun=cost_fun, solve=True, 
					maxIter=20, epsTol=1e-5, lamda=100, devM=0.5, rho0=1e-2, 
					rho1=0.75, rho2=0.95, alpha=2.0, beta=1.5,
					timeLimit=5, n_thread=1, verbose=False, autotune=False)
    print(ljRes)
    print(optCost, status, solveTime)
    maxDiff = 0
    for s_id in swarm_ids:
    	for n_id in node_ids:
    		for t in range(Kp):
    			s = sum(MDictRes[(s_id, n_id, m_id, t)]*xDictRes[(s_id, m_id,t)] for m_id in node_ids)
    			# print(np.abs(xDictRes[(s_id, n_id,t+1)]-s))
    			maxDiff = np.maximum(maxDiff, np.abs(xDictRes[(s_id, n_id,t+1)]-s))
    print(maxDiff)