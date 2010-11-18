import base
from util import Traversable
from StringIO import StringIO					# need a dict to set bloody .name field
from git.util import Iterable, join_path_native, to_native_path_linux
from git.config import GitConfigParser, SectionConstraint
from git.exc import InvalidGitRepositoryError, NoSuchPathError
import stat
import git

import os
import sys
import weakref
import shutil

__all__ = ("Submodule", "RootModule")

#{ Utilities

def sm_section(name):
	""":return: section title used in .gitmodules configuration file"""
	return 'submodule "%s"' % name

def sm_name(section):
	""":return: name of the submodule as parsed from the section name"""
	section = section.strip()
	return section[11:-1]
	
def mkhead(repo, path):
	""":return: New branch/head instance"""
	return git.Head(repo, git.Head.to_full_path(path))
	
def unbare_repo(func):
	"""Methods with this decorator raise InvalidGitRepositoryError if they 
	encounter a bare repository"""
	def wrapper(self, *args, **kwargs):
		if self.repo.bare:
			raise InvalidGitRepositoryError("Method '%s' cannot operate on bare repositories" % func.__name__)
		#END bare method
		return func(self, *args, **kwargs)
	# END wrapper
	wrapper.__name__ = func.__name__
	return wrapper
	
def find_first_remote_branch(remotes, branch):
	"""Find the remote branch matching the name of the given branch or raise InvalidGitRepositoryError"""
	for remote in remotes:
		try:
			return remote.refs[branch.name]
		except IndexError:
			continue
		# END exception handling
	#END for remote
	raise InvalidGitRepositoryError("Didn't find remote branch %r in any of the given remotes", branch)
	
#} END utilities


#{ Classes

class SubmoduleConfigParser(GitConfigParser):
	"""
	Catches calls to _write, and updates the .gitmodules blob in the index
	with the new data, if we have written into a stream. Otherwise it will 
	add the local file to the index to make it correspond with the working tree.
	Additionally, the cache must be cleared
	
	Please note that no mutating method will work in bare mode
	"""
	
	def __init__(self, *args, **kwargs):
		self._smref = None
		self._index = None
		self._auto_write = True
		super(SubmoduleConfigParser, self).__init__(*args, **kwargs)
	
	#{ Interface
	def set_submodule(self, submodule):
		"""Set this instance's submodule. It must be called before 
		the first write operation begins"""
		self._smref = weakref.ref(submodule)

	def flush_to_index(self):
		"""Flush changes in our configuration file to the index"""
		assert self._smref is not None
		# should always have a file here
		assert not isinstance(self._file_or_files, StringIO)
		
		sm = self._smref()
		if sm is not None:
			index = self._index
			if index is None:
				index = sm.repo.index
			# END handle index
			index.add([sm.k_modules_file], write=self._auto_write)
			sm._clear_cache()
		# END handle weakref

	#} END interface
	
	#{ Overridden Methods
	def write(self):
		rval = super(SubmoduleConfigParser, self).write()
		self.flush_to_index()
		return rval
	# END overridden methods


class Submodule(base.IndexObject, Iterable, Traversable):
	"""Implements access to a git submodule. They are special in that their sha
	represents a commit in the submodule's repository which is to be checked out
	at the path of this instance. 
	The submodule type does not have a string type associated with it, as it exists
	solely as a marker in the tree and index.
	
	All methods work in bare and non-bare repositories."""
	
	_id_attribute_ = "name"
	k_modules_file = '.gitmodules'
	k_head_option = 'branch'
	k_head_default = 'master'
	k_default_mode = stat.S_IFDIR | stat.S_IFLNK		# submodules are directories with link-status
	
	# this is a bogus type for base class compatability
	type = 'submodule'
	
	__slots__ = ('_parent_commit', '_url', '_branch', '_name', '__weakref__')
	_cache_attrs = ('path', '_url', '_branch')
	
	def __init__(self, repo, binsha, mode=None, path=None, name = None, parent_commit=None, url=None, branch=None):
		"""Initialize this instance with its attributes. We only document the ones 
		that differ from ``IndexObject``
		:param repo: Our parent repository
		:param binsha: binary sha referring to a commit in the remote repository, see url parameter
		:param parent_commit: see set_parent_commit()
		:param url: The url to the remote repository which is the submodule
		:param branch: Head instance to checkout when cloning the remote repository"""
		super(Submodule, self).__init__(repo, binsha, mode, path)
		self.size = 0
		if parent_commit is not None:
			self._parent_commit = parent_commit
		if url is not None:
			self._url = url
		if branch is not None:
			assert isinstance(branch, git.Head)
			self._branch = branch
		if name is not None:
			self._name = name
	
	def _set_cache_(self, attr):
		if attr == '_parent_commit':
			# set a default value, which is the root tree of the current head
			self._parent_commit = self.repo.commit()
		elif attr in ('path', '_url', '_branch'):
			reader = self.config_reader()
			# default submodule values
			self.path = reader.get_value('path')
			self._url = reader.get_value('url')
			# git-python extension values - optional
			self._branch = mkhead(self.repo, reader.get_value(self.k_head_option, self.k_head_default))
		elif attr == '_name':
			raise AttributeError("Cannot retrieve the name of a submodule if it was not set initially")
		else:
			super(Submodule, self)._set_cache_(attr)
		# END handle attribute name
		
	def _get_intermediate_items(self, item):
		""":return: all the submodules of our module repository"""
		try:
			return type(self).list_items(item.module())
		except InvalidGitRepositoryError:
			return list()
		# END handle intermeditate items
		
	def __eq__(self, other):
		"""Compare with another submodule"""
		# we may only compare by name as this should be the ID they are hashed with
		# Otherwise this type wouldn't be hashable
		# return self.path == other.path and self.url == other.url and super(Submodule, self).__eq__(other)
		return self._name == other._name
		
	def __ne__(self, other):
		"""Compare with another submodule for inequality"""
		return not (self == other)
		
	def __hash__(self):
		"""Hash this instance using its logical id, not the sha"""
		return hash(self._name)
		
	def __str__(self):
		return self._name
		
	def __repr__(self):
		return "git.%s(name=%s, path=%s, url=%s, branch=%s)" % (type(self).__name__, self._name, self.path, self.url, self.branch) 
		
	@classmethod
	def _config_parser(cls, repo, parent_commit, read_only):
		""":return: Config Parser constrained to our submodule in read or write mode
		:raise IOError: If the .gitmodules file cannot be found, either locally or in the repository
			at the given parent commit. Otherwise the exception would be delayed until the first 
			access of the config parser"""
		parent_matches_head = repo.head.commit == parent_commit
		if not repo.bare and parent_matches_head:
			fp_module = cls.k_modules_file
			fp_module_path = os.path.join(repo.working_tree_dir, fp_module)
			if not os.path.isfile(fp_module_path):
				raise IOError("%s file was not accessible" % fp_module_path)
			# END handle existance
			fp_module = fp_module_path
		else:
			try:
				fp_module = cls._sio_modules(parent_commit)
			except KeyError:
				raise IOError("Could not find %s file in the tree of parent commit %s" % (cls.k_modules_file, parent_commit))
			# END handle exceptions
		# END handle non-bare working tree
		
		if not read_only and (repo.bare or not parent_matches_head):
			raise ValueError("Cannot write blobs of 'historical' submodule configurations")
		# END handle writes of historical submodules
		
		return SubmoduleConfigParser(fp_module, read_only = read_only)

	def _clear_cache(self):
		# clear the possibly changed values
		for name in self._cache_attrs:
			try:
				delattr(self, name)
			except AttributeError:
				pass
			# END try attr deletion
		# END for each name to delete
		
	@classmethod
	def _sio_modules(cls, parent_commit):
		""":return: Configuration file as StringIO - we only access it through the respective blob's data"""
		sio = StringIO(parent_commit.tree[cls.k_modules_file].data_stream.read())
		sio.name = cls.k_modules_file
		return sio
	
	def _config_parser_constrained(self, read_only):
		""":return: Config Parser constrained to our submodule in read or write mode"""
		parser = self._config_parser(self.repo, self._parent_commit, read_only)
		parser.set_submodule(self)
		return SectionConstraint(parser, sm_section(self.name))
		
	#{ Edit Interface
	
	@classmethod
	def add(cls, repo, name, path, url=None, branch=None, no_checkout=False):
		"""Add a new submodule to the given repository. This will alter the index
		as well as the .gitmodules file, but will not create a new commit.
		If the submodule already exists, no matter if the configuration differs
		from the one provided, the existing submodule will be returned.
		:param repo: Repository instance which should receive the submodule
		:param name: The name/identifier for the submodule
		:param path: repository-relative or absolute path at which the submodule 
			should be located
			It will be created as required during the repository initialization.
		:param url: git-clone compatible URL, see git-clone reference for more information
			If None, the repository is assumed to exist, and the url of the first
			remote is taken instead. This is useful if you want to make an existing
			repository a submodule of anotherone.
		:param branch: branch at which the submodule should (later) be checked out.
			The given branch must exist in the remote repository, and will be checked
			out locally as a tracking branch.
			It will only be written into the configuration if it not None, which is
			when the checked out branch will be the one the remote HEAD pointed to.
			The result you get in these situation is somewhat fuzzy, and it is recommended
			to specify at least 'master' here
		:param no_checkout: if True, and if the repository has to be cloned manually, 
			no checkout will be performed
		:return: The newly created submodule instance
		:note: works atomically, such that no change will be done if the repository
			update fails for instance"""
		if repo.bare:
			raise InvalidGitRepositoryError("Cannot add submodules to bare repositories")
		# END handle bare repos
		
		path = to_native_path_linux(path)
		if path.endswith('/'):
			path = path[:-1]
		# END handle trailing slash
		
		# INSTANTIATE INTERMEDIATE SM
		sm = cls(repo, cls.NULL_BIN_SHA, cls.k_default_mode, path, name)
		if sm.exists():
			# reretrieve submodule from tree
			return repo.head.commit.tree[path]
		# END handle existing
		
		br = mkhead(repo, branch or cls.k_head_default)
		has_module = sm.module_exists()
		branch_is_default = branch is None
		if has_module and url is not None:
			if url not in [r.url for r in sm.module().remotes]:
				raise ValueError("Specified URL '%s' does not match any remote url of the repository at '%s'" % (url, sm.abspath))
			# END check url
		# END verify urls match
		
		mrepo = None
		if url is None:
			if not has_module:
				raise ValueError("A URL was not given and existing repository did not exsit at %s" % path)
			# END check url
			mrepo = sm.module()
			urls = [r.url for r in mrepo.remotes]
			if not urls:
				raise ValueError("Didn't find any remote url in repository at %s" % sm.abspath)
			# END verify we have url
			url = urls[0]
		else:
			# clone new repo
			kwargs = {'n' : no_checkout}
			if not branch_is_default:
				kwargs['b'] = str(br)
			# END setup checkout-branch
			mrepo = git.Repo.clone_from(url, path, **kwargs)
		# END verify url
		
		# update configuration and index
		index = sm.repo.index
		writer = sm.config_writer(index=index, write=False)
		writer.set_value('url', url)
		writer.set_value('path', path)
		
		sm._url = url
		if not branch_is_default:
			# store full path
			writer.set_value(cls.k_head_option, br.path)
			sm._branch = br.path
		# END handle path
		del(writer)
		
		# NOTE: Have to write the repo config file as well, otherwise
		# the default implementation will be offended and not update the repository
		# Maybe this is a good way to assure it doesn't get into our way, but 
		# we want to stay backwards compatible too ... . Its so redundant !
		repo.config_writer().set_value(sm_section(sm.name), 'url', url)
		
		# we deliberatly assume that our head matches our index !
		pcommit = repo.head.commit
		sm._parent_commit = pcommit
		sm.binsha = mrepo.head.commit.binsha
		index.add([sm], write=True)
		
		return sm
		
	def update(self, recursive=False, init=True, to_latest_revision=False):
		"""Update the repository of this submodule to point to the checkout
		we point at with the binsha of this instance.
		:param recursive: if True, we will operate recursively and update child-
			modules as well.
		:param init: if True, the module repository will be cloned into place if necessary
		:param to_latest_revision: if True, the submodule's sha will be ignored during checkout.
			Instead, the remote will be fetched, and the local tracking branch updated.
			This only works if we have a local tracking branch, which is the case
			if the remote repository had a master branch, or of the 'branch' option 
			was specified for this submodule and the branch existed remotely
		:note: does nothing in bare repositories
		:note: method is definitely not atomic if recurisve is True
		:return: self"""
		if self.repo.bare:
			return self
		#END pass in bare mode
		
		
		# ASSURE REPO IS PRESENT AND UPTODATE
		#####################################
		try:
			mrepo = self.module()
			for remote in mrepo.remotes:
				remote.fetch()
			#END fetch new data
		except InvalidGitRepositoryError:
			if not init:
				return self
			# END early abort if init is not allowed
			import git
			
			# there is no git-repository yet - but delete empty paths
			module_path = join_path_native(self.repo.working_tree_dir, self.path)
			if os.path.isdir(module_path):
				try:
					os.rmdir(module_path)
				except OSError:
					raise OSError("Module directory at %r does already exist and is non-empty" % module_path)
				# END handle OSError
			# END handle directory removal
			
			# don't check it out at first - nonetheless it will create a local
			# branch according to the remote-HEAD if possible
			mrepo = git.Repo.clone_from(self.url, module_path, n=True)
			
			# see whether we have a valid branch to checkout
			try:
				# find  a remote which has our branch - we try to be flexible
				remote_branch = find_first_remote_branch(mrepo.remotes, self.branch)
				local_branch = self.branch
				if not local_branch.is_valid():
					# Setup a tracking configuration - branch doesn't need to 
					# exist to do that
					local_branch.set_tracking_branch(remote_branch)
				#END handle local branch
				
				# have a valid branch, but no checkout - make sure we can figure
				# that out by marking the commit with a null_sha
				# have to write it directly as .commit = NULLSHA tries to resolve the sha
				# This will bring the branch into existance
				refpath = join_path_native(mrepo.git_dir, local_branch.path)
				refdir = os.path.dirname(refpath)
				if not os.path.isdir(refdir):
					os.makedirs(refdir)
				#END handle directory
				open(refpath, 'w').write(self.NULL_HEX_SHA)
				# END initial checkout + branch creation
				
				# make sure HEAD is not detached
				mrepo.head.ref = local_branch
			except IndexError:
				print >> sys.stderr, "Warning: Failed to checkout tracking branch %s" % self.branch 
			#END handle tracking branch
		#END handle initalization
		
		
		# DETERMINE SHAS TO CHECKOUT
		############################
		binsha = self.binsha
		hexsha = self.hexsha
		is_detached = mrepo.head.is_detached
		if to_latest_revision:
			msg_base = "Cannot update to latest revision in repository at %r as " % mrepo.working_dir
			if not is_detached:
				rref = mrepo.head.ref.tracking_branch()
				if rref is not None:
					rcommit = rref.commit
					binsha = rcommit.binsha
					hexsha = rcommit.hexsha
				else:
					print >> sys.stderr, "%s a tracking branch was not set for local branch '%s'" % (msg_base, mrepo.head.ref) 
				# END handle remote ref
			else:
				print >> sys.stderr, "%s there was no local tracking branch" % msg_base
			# END handle detached head
		# END handle to_latest_revision option
		
		# update the working tree
		if mrepo.head.commit.binsha != binsha:
			if is_detached:
				mrepo.git.checkout(hexsha)
			else:
				# TODO: allow to specify a rebase, merge, or reset
				# TODO: Warn if the hexsha forces the tracking branch off the remote
				# branch - this should be prevented when setting the branch option
				mrepo.head.reset(hexsha, index=True, working_tree=True)
			# END handle checkout
		# END update to new commit only if needed
		
		# HANDLE RECURSION
		##################
		if recursive:
			for submodule in self.iter_items(self.module()):
				submodule.update(recursive, init, to_latest_revision)
			# END handle recursive update
		# END for each submodule
			
		return self
		
	@unbare_repo
	def move(self, module_path, configuration=True, module=True):
		"""Move the submodule to a another module path. This involves physically moving
		the repository at our current path, changing the configuration, as well as
		adjusting our index entry accordingly.
		:param module_path: the path to which to move our module, given as
			repository-relative path. Intermediate directories will be created
			accordingly. If the path already exists, it must be empty.
			Trailling (back)slashes are removed automatically
		:param configuration: if True, the configuration will be adjusted to let 
			the submodule point to the given path.
		:param module: if True, the repository managed by this submodule
			will be moved, not the configuration. This will effectively 
			leave your repository in an inconsistent state unless the configuration
			and index already point to the target location.
		:return: self
		:raise ValueError: if the module path existed and was not empty, or was a file
		:note: Currently the method is not atomic, and it could leave the repository
			in an inconsistent state if a sub-step fails for some reason
		"""
		if module + configuration < 1:
			raise ValueError("You must specify to move at least the module or the configuration of the submodule")
		#END handle input
		
		module_path = to_native_path_linux(module_path)
		if module_path.endswith('/'):
			module_path = module_path[:-1]
		# END handle trailing slash
		
		# VERIFY DESTINATION
		if module_path == self.path:
			return self
		#END handle no change
		
		dest_path = join_path_native(self.repo.working_tree_dir, module_path)
		if os.path.isfile(dest_path):
			raise ValueError("Cannot move repository onto a file: %s" % dest_path)
		# END handle target files
		
		index = self.repo.index
		tekey = index.entry_key(module_path, 0)
		# if the target item already exists, fail
		if configuration and tekey in index.entries:
			raise ValueError("Index entry for target path did alredy exist")
		#END handle index key already there
		
		# remove existing destination
		if module:
			if os.path.exists(dest_path):
				if len(os.listdir(dest_path)):
					raise ValueError("Destination module directory was not empty")
				#END handle non-emptyness
				
				if os.path.islink(dest_path):
					os.remove(dest_path)
				else:
					os.rmdir(dest_path)
				#END handle link
			else:
				# recreate parent directories
				# NOTE: renames() does that now
				pass
			#END handle existance
		# END handle module
		
		# move the module into place if possible
		cur_path = self.abspath
		renamed_module = False
		if module and os.path.exists(cur_path):
			os.renames(cur_path, dest_path)
			renamed_module = True
		#END move physical module
		
		
		# rename the index entry - have to manipulate the index directly as 
		# git-mv cannot be used on submodules ... yeah
		try:
			if configuration:
				try:
					ekey = index.entry_key(self.path, 0)
					entry = index.entries[ekey]
					del(index.entries[ekey])
					nentry = git.IndexEntry(entry[:3]+(module_path,)+entry[4:])
					index.entries[tekey] = nentry
				except KeyError:
					raise InvalidGitRepositoryError("Submodule's entry at %r did not exist" % (self.path))
				#END handle submodule doesn't exist
				
				# update configuration
				writer = self.config_writer(index=index)		# auto-write
				writer.set_value('path', module_path)
				self.path = module_path
				del(writer)
			# END handle configuration flag
		except Exception:
			if renamed_module:
				os.renames(dest_path, cur_path)
			# END undo module renaming
			raise
		#END handle undo rename
		
		return self
		
	@unbare_repo
	def remove(self, module=True, force=False, configuration=True, dry_run=False):
		"""Remove this submodule from the repository. This will remove our entry
		from the .gitmodules file and the entry in the .git/config file.
		:param module: If True, the module we point to will be deleted 
			as well. If the module is currently on a commit which is not part 
			of any branch in the remote, if the currently checked out branch 
			is ahead of its tracking branch,  if you have modifications in the
			working tree, or untracked files,
			In case the removal of the repository fails for these reasons, the 
			submodule status will not have been altered.
			If this submodule has child-modules on its own, these will be deleted
			prior to touching the own module.
		:param force: Enforces the deletion of the module even though it contains 
			modifications. This basically enforces a brute-force file system based
			deletion.
		:param configuration: if True, the submodule is deleted from the configuration, 
			otherwise it isn't. Although this should be enabled most of the times, 
			this flag enables you to safely delete the repository of your submodule.
		:param dry_run: if True, we will not actually do anything, but throw the errors
			we would usually throw
		:return: self
		:note: doesn't work in bare repositories
		:raise InvalidGitRepositoryError: thrown if the repository cannot be deleted
		:raise OSError: if directories or files could not be removed"""
		if not (module + configuration):
			raise ValueError("Need to specify to delete at least the module, or the configuration")
		# END handle params
		
		# DELETE MODULE REPOSITORY
		##########################
		if module and self.module_exists():
			if force:
				# take the fast lane and just delete everything in our module path
				# TODO: If we run into permission problems, we have a highly inconsistent
				# state. Delete the .git folders last, start with the submodules first
				mp = self.abspath
				method = None
				if os.path.islink(mp):
					method = os.remove
				elif os.path.isdir(mp):
					method = shutil.rmtree
				elif os.path.exists(mp):
					raise AssertionError("Cannot forcibly delete repository as it was neither a link, nor a directory")
				#END handle brutal deletion
				if not dry_run:
					assert method
					method(mp)
				#END apply deletion method
			else:
				# verify we may delete our module
				mod = self.module()
				if mod.is_dirty(untracked_files=True):
					raise InvalidGitRepositoryError("Cannot delete module at %s with any modifications, unless force is specified" % mod.working_tree_dir)
				# END check for dirt
				
				# figure out whether we have new commits compared to the remotes
				# NOTE: If the user pulled all the time, the remote heads might 
				# not have been updated, so commits coming from the remote look
				# as if they come from us. But we stay strictly read-only and
				# don't fetch beforhand.
				for remote in mod.remotes:
					num_branches_with_new_commits = 0
					rrefs = remote.refs
					for rref in rrefs:
						num_branches_with_new_commits = len(mod.git.cherry(rref)) != 0
					# END for each remote ref
					# not a single remote branch contained all our commits
					if num_branches_with_new_commits == len(rrefs):
						raise InvalidGitRepositoryError("Cannot delete module at %s as there are new commits" % mod.working_tree_dir)
					# END handle new commits
				# END for each remote
				
				# gently remove all submodule repositories
				for sm in self.children():
					sm.remove(module=True, force=False, configuration=False, dry_run=dry_run)
				# END for each child-submodule
				
				# finally delete our own submodule
				if not dry_run:
					shutil.rmtree(mod.working_tree_dir)
				# END delete tree if possible
			# END handle force
		# END handle module deletion
			
		# DELETE CONFIGURATION
		######################
		if configuration and not dry_run:
			# first the index-entry
			index = self.repo.index
			try:
				del(index.entries[index.entry_key(self.path, 0)])
			except KeyError:
				pass
			#END delete entry
			index.write()
			
			# now git config - need the config intact, otherwise we can't query 
			# inforamtion anymore
			self.repo.config_writer().remove_section(sm_section(self.name))
			self.config_writer().remove_section()
		# END delete configuration
		
		return self
		
	def set_parent_commit(self, commit, check=True):
		"""Set this instance to use the given commit whose tree is supposed to 
		contain the .gitmodules blob.
		:param commit: Commit'ish reference pointing at the root_tree
		:param check: if True, relatively expensive checks will be performed to verify
			validity of the submodule.
		:raise ValueError: if the commit's tree didn't contain the .gitmodules blob.
		:raise ValueError: if the parent commit didn't store this submodule under the
			current path
		:return: self"""
		pcommit = self.repo.commit(commit)
		pctree = pcommit.tree
		if self.k_modules_file not in pctree:
			raise ValueError("Tree of commit %s did not contain the %s file" % (commit, self.k_modules_file))
		# END handle exceptions
		
		prev_pc = self._parent_commit
		self._parent_commit = pcommit
		
		if check:
			parser = self._config_parser(self.repo, self._parent_commit, read_only=True)
			if not parser.has_section(sm_section(self.name)):
				self._parent_commit = prev_pc
				raise ValueError("Submodule at path %r did not exist in parent commit %s" % (self.path, commit)) 
			# END handle submodule did not exist
		# END handle checking mode
		
		# update our sha, it could have changed
		self.binsha = pctree[self.path].binsha
		
		self._clear_cache()
		
		return self
		
	@unbare_repo
	def config_writer(self, index=None, write=True):
		""":return: a config writer instance allowing you to read and write the data
		belonging to this submodule into the .gitmodules file.
		
		:param index: if not None, an IndexFile instance which should be written.
			defaults to the index of the Submodule's parent repository.
		:param write: if True, the index will be written each time a configuration
			value changes.
		:note: the parameters allow for a more efficient writing of the index, 
			as you can pass in a modified index on your own, prevent automatic writing, 
			and write yourself once the whole operation is complete
		:raise ValueError: if trying to get a writer on a parent_commit which does not
			match the current head commit
		:raise IOError: If the .gitmodules file/blob could not be read"""
		writer = self._config_parser_constrained(read_only=False)
		if index is not None:
			writer.config._index = index
		writer.config._auto_write = write
		return writer
		
	#} END edit interface
	
	#{ Query Interface
	
	@unbare_repo
	def module(self):
		""":return: Repo instance initialized from the repository at our submodule path
		:raise InvalidGitRepositoryError: if a repository was not available. This could 
			also mean that it was not yet initialized"""
		# late import to workaround circular dependencies
		module_path = self.abspath 
		try:
			repo = git.Repo(module_path)
			if repo != self.repo:
				return repo
			# END handle repo uninitialized
		except (InvalidGitRepositoryError, NoSuchPathError):
			raise InvalidGitRepositoryError("No valid repository at %s" % self.path)
		else:
			raise InvalidGitRepositoryError("Repository at %r was not yet checked out" % module_path)
		# END handle exceptions
		
	def module_exists(self):
		""":return: True if our module exists and is a valid git repository. See module() method"""
		try:
			self.module()
			return True
		except Exception:
			return False
		# END handle exception
	
	def exists(self):
		""":return: True if the submodule exists, False otherwise. Please note that
		a submodule may exist (in the .gitmodules file) even though its module
		doesn't exist"""
		# keep attributes for later, and restore them if we have no valid data
		# this way we do not actually alter the state of the object
		loc = locals()
		for attr in self._cache_attrs:
			if hasattr(self, attr):
				loc[attr] = getattr(self, attr)
			# END if we have the attribute cache
		#END for each attr
		self._clear_cache()
		
		try:
			try:
				self.path
				return True
			except Exception:
				return False
			# END handle exceptions
		finally:
			for attr in self._cache_attrs:
				if attr in loc:
					setattr(self, attr, loc[attr])
				# END if we have a cache
			# END reapply each attribute
		# END handle object state consistency
	
	@property
	def branch(self):
		""":return: The branch instance that we are to checkout"""
		return self._branch
	
	@property
	def url(self):
		""":return: The url to the repository which our module-repository refers to"""
		return self._url
	
	@property
	def parent_commit(self):
		""":return: Commit instance with the tree containing the .gitmodules file
		:note: will always point to the current head's commit if it was not set explicitly"""
		return self._parent_commit
		
	@property
	def name(self):
		""":return: The name of this submodule. It is used to identify it within the 
			.gitmodules file.
		:note: by default, the name is the path at which to find the submodule, but
			in git-python it should be a unique identifier similar to the identifiers
			used for remotes, which allows to change the path of the submodule
			easily
		"""
		return self._name
	
	def config_reader(self):
		""":return: ConfigReader instance which allows you to qurey the configuration values
		of this submodule, as provided by the .gitmodules file
		:note: The config reader will actually read the data directly from the repository
			and thus does not need nor care about your working tree.
		:note: Should be cached by the caller and only kept as long as needed
		:raise IOError: If the .gitmodules file/blob could not be read"""
		return self._config_parser_constrained(read_only=True)
		
	def children(self):
		""":return: IterableList(Submodule, ...) an iterable list of submodules instances
		which are children of this submodule
		:raise InvalidGitRepositoryError: if the submodule is not checked-out"""
		return self._get_intermediate_items(self)
		
	#} END query interface
	
	#{ Iterable Interface
	
	@classmethod
	def iter_items(cls, repo, parent_commit='HEAD'):
		""":return: iterator yielding Submodule instances available in the given repository"""
		pc = repo.commit(parent_commit)			# parent commit instance
		try:
			parser = cls._config_parser(repo, pc, read_only=True)
		except IOError:
			raise StopIteration
		# END handle empty iterator
		
		rt = pc.tree								# root tree
		
		for sms in parser.sections():
			n = sm_name(sms)
			p = parser.get_value(sms, 'path')
			u = parser.get_value(sms, 'url')
			b = cls.k_head_default
			if parser.has_option(sms, cls.k_head_option):
				b = parser.get_value(sms, cls.k_head_option)
			# END handle optional information
			
			# get the binsha
			index = repo.index
			try:
				sm = rt[p]
			except KeyError:
				# try the index, maybe it was just added
				try:
					entry = index.entries[index.entry_key(p, 0)]
					sm = cls(repo, entry.binsha, entry.mode, entry.path)
				except KeyError:
					raise InvalidGitRepositoryError("Gitmodule path %r did not exist in revision of parent commit %s" % (p, parent_commit))
				# END handle keyerror
			# END handle critical error
			
			# fill in remaining info - saves time as it doesn't have to be parsed again
			sm._name = n
			sm._parent_commit = pc
			sm._branch = mkhead(repo, b)
			sm._url = u
			
			yield sm
		# END for each section
	
	#} END iterable interface
	
	
class RootModule(Submodule):
	"""A (virtual) Root of all submodules in the given repository. It can be used
	to more easily traverse all submodules of the master repository"""
	
	__slots__ = tuple()
	
	k_root_name = '__ROOT__'
	
	def __init__(self, repo):
		# repo, binsha, mode=None, path=None, name = None, parent_commit=None, url=None, ref=None)
		super(RootModule, self).__init__(
										repo, 
										binsha = self.NULL_BIN_SHA, 
										mode = self.k_default_mode, 
										path = '', 
										name = self.k_root_name, 
										parent_commit = repo.head.commit,
										url = '',
										branch = mkhead(repo, self.k_head_default)
										)
		
	
	def _clear_cache(self):
		"""May not do anything"""
		pass
	
	#{ Interface 
	
	def update(self, previous_commit=None, recursive=True, force_remove=False, init=True, to_latest_revision=False):
		"""Update the submodules of this repository to the current HEAD commit.
		This method behaves smartly by determining changes of the path of a submodules
		repository, next to changes to the to-be-checked-out commit or the branch to be 
		checked out. This works if the submodules ID does not change.
		Additionally it will detect addition and removal of submodules, which will be handled
		gracefully.
		
		:param previous_commit: If set to a commit'ish, the commit we should use 
			as the previous commit the HEAD pointed to before it was set to the commit it points to now. 
			If None, it defaults to ORIG_HEAD otherwise, or the parent of the current
			commit if it is not given
		:param recursive: if True, the children of submodules will be updated as well
			using the same technique
		:param force_remove: If submodules have been deleted, they will be forcibly removed.
			Otherwise the update may fail if a submodule's repository cannot be deleted as 
			changes have been made to it (see Submodule.update() for more information)
		:param init: If we encounter a new module which would need to be initialized, then do it.
		:param to_latest_revision: If True, instead of checking out the revision pointed to 
			by this submodule's sha, the checked out tracking branch will be merged with the 
			newest remote branch fetched from the repository's origin"""
		if self.repo.bare:
			raise InvalidGitRepositoryError("Cannot update submodules in bare repositories")
		# END handle bare
		
		repo = self.repo
		
		# HANDLE COMMITS
		##################
		cur_commit = repo.head.commit
		if previous_commit is None:
			symref = repo.head.orig_head()
			try:
				previous_commit = symref.commit
			except Exception:
				pcommits = cur_commit.parents
				if pcommits:
					previous_commit = pcommits[0]
				else:
					# in this special case, we just diff against ourselve, which
					# means exactly no change
					previous_commit = cur_commit
				# END handle initial commit
			# END no ORIG_HEAD
		else:
			previous_commit = repo.commit(previous_commit)	 # obtain commit object 
		# END handle previous commit
		
		
		psms = self.list_items(repo, parent_commit=previous_commit)
		sms = self.list_items(self.module())
		spsms = set(psms)
		ssms = set(sms)
		
		# HANDLE REMOVALS
		###################
		for rsm in (spsms - ssms):
			# fake it into thinking its at the current commit to allow deletion
			# of previous module. Trigger the cache to be updated before that
			#rsm.url
			rsm._parent_commit = repo.head.commit
			rsm.remove(configuration=False, module=True, force=force_remove)
		# END for each removed submodule
		
		# HANDLE PATH RENAMES
		#####################
		# url changes + branch changes
		for csm in (spsms & ssms):
			psm = psms[csm.name]
			sm = sms[csm.name]
			
			if sm.path != psm.path and psm.module_exists():
				# move the module to the new path
				psm.move(sm.path, module=True, configuration=False)
			# END handle path changes
			
			if sm.module_exists():
				# handle url change
				if sm.url != psm.url:
					# Add the new remote, remove the old one
					# This way, if the url just changes, the commits will not 
					# have to be re-retrieved
					nn = '__new_origin__'
					smm = sm.module()
					rmts = smm.remotes
					
					# don't do anything if we already have the url we search in place
					if len([r for r in rmts if r.url == sm.url]) == 0:
						
						
						assert nn not in [r.name for r in rmts]
						smr = smm.create_remote(nn, sm.url)
						smr.fetch()
						
						# If we have a tracking branch, it should be available
						# in the new remote as well.
						if len([r for r in smr.refs if r.remote_head == sm.branch.name]) == 0:
							raise ValueError("Submodule branch named %r was not available in new submodule remote at %r" % (sm.branch.name, sm.url))
						# END head is not detached
						
						# now delete the changed one
						rmt_for_deletion = None
						for remote in rmts:
							if remote.url == psm.url:
								rmt_for_deletion = remote
								break
							# END if urls match
						# END for each remote
						
						# if we didn't find a matching remote, but have exactly one, 
						# we can safely use this one
						if rmt_for_deletion is None:
							if len(rmts) == 1:
								rmt_for_deletion = rmts[0]
							else:
								# if we have not found any remote with the original url
								# we may not have a name. This is a special case, 
								# and its okay to fail here
								# Alternatively we could just generate a unique name and leave all
								# existing ones in place
								raise InvalidGitRepositoryError("Couldn't find original remote-repo at url %r" % psm.url)
							#END handle one single remote
						# END handle check we found a remote
						
						orig_name = rmt_for_deletion.name
						smm.delete_remote(rmt_for_deletion)
						# NOTE: Currently we leave tags from the deleted remotes
						# as well as separate tracking branches in the possibly totally 
						# changed repository ( someone could have changed the url to 
						# another project ). At some point, one might want to clean
						# it up, but the danger is high to remove stuff the user
						# has added explicitly
						
						# rename the new remote back to what it was
						smr.rename(orig_name)
						
						# early on, we verified that the our current tracking branch
						# exists in the remote. Now we have to assure that the 
						# sha we point to is still contained in the new remote
						# tracking branch.
						smsha = sm.binsha
						found = False
						rref = smr.refs[self.branch.name]
						for c in rref.commit.traverse():
							if c.binsha == smsha:
								found = True
								break
							# END traverse all commits in search for sha
						# END for each commit
						
						if not found:
							# adjust our internal binsha to use the one of the remote
							# this way, it will be checked out in the next step
							# This will change the submodule relative to us, so 
							# the user will be able to commit the change easily
							print >> sys.stderr, "WARNING: Current sha %s was not contained in the tracking branch at the new remote, setting it the the remote's tracking branch" % sm.hexsha
							sm.binsha = rref.commit.binsha
						#END reset binsha
						
						#NOTE: All checkout is performed by the base implementation of update
						
					# END skip remote handling if new url already exists in module
				# END handle url
				
				if sm.branch != psm.branch:
					# finally, create a new tracking branch which tracks the 
					# new remote branch
					smm = sm.module()
					smmr = smm.remotes
					try:
						tbr = git.Head.create(smm, sm.branch.name)
					except git.GitCommandError, e:
						if e.status != 128:
							raise
						#END handle something unexpected
						
						# ... or reuse the existing one
						tbr = git.Head(smm, git.Head.to_full_path(sm.branch.name))
					#END assure tracking branch exists
					
					tbr.set_tracking_branch(find_first_remote_branch(smmr, sm.branch))
					# figure out whether the previous tracking branch contains
					# new commits compared to the other one, if not we can 
					# delete it.
					try:
						tbr = find_first_remote_branch(smmr, psm.branch)
						if len(smm.git.cherry(tbr, psm.branch)) == 0:
							psm.branch.delete(smm, psm.branch)
						#END delete original tracking branch if there are no changes
					except InvalidGitRepositoryError:
						# ignore it if the previous branch couldn't be found in the
						# current remotes, this just means we can't handle it
						pass
					# END exception handling
					
					#NOTE: All checkout is done in the base implementation of update
					
				#END handle branch
			#END handle 
		# END for each common submodule 
		
		# FINALLY UPDATE ALL ACTUAL SUBMODULES
		######################################
		for sm in sms:
			# update the submodule using the default method
			sm.update(recursive=True, init=init, to_latest_revision=to_latest_revision)
			
			# update recursively depth first - question is which inconsitent 
			# state will be better in case it fails somewhere. Defective branch
			# or defective depth. The RootSubmodule type will never process itself, 
			# which was done in the previous expression
			if recursive:
				type(self)(sm.module()).update(recursive=True, force_remove=force_remove, 
											init=init, to_latest_revision=to_latest_revision)
			#END handle recursive
		# END for each submodule to update

	def module(self):
		""":return: the actual repository containing the submodules"""
		return self.repo
	#} END interface
#} END classes
