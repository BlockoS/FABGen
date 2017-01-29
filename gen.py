from pypeg2 import re, flag, name, Plain, optional, attr, K, parse
import copy


#
typename = re.compile(r"((::)*(_|[A-z])[A-z0-9_]*)+")
ref_re = re.compile(r"[&*]+")


def get_fully_qualified_ctype_name(type):
	out = ''
	if type.const:
		out += 'const '
	out += type.unqualified_name
	if hasattr(type, 'ref'):
		out += ' ' + type.ref
	return out


def get_type_clean_name(type):
	""" Return a type name cleaned so that it may be used as variable name in the generator output."""
	parts = type.split(' ')

	def clean_type_name_part(part):
		part = part.replace('*', 'ptr')  # pointer
		part = part.replace('&', '_r')  # reference
		part = part.replace('::', '__')  # namespace
		return part

	parts = [clean_type_name_part(part) for part in parts]
	return '_'.join(parts)


def ctypes_to_string(ctypes):
	return ','.join([repr(ctype) for ctype in ctypes])


class _CType:
	def __repr__(self):
		return get_fully_qualified_ctype_name(self)

	def get_ref(self, extra_transform=''):
		return (self.ref if hasattr(self, 'ref') else '') + extra_transform

	def add_ref(self, ref):
		t = copy.deepcopy(self)
		if hasattr(self, 'ref'):
			t.ref += ref
		else:
			setattr(t, 'ref', ref)
		return t


_CType.grammar = flag("const", K("const")), optional([flag("signed", K("signed")), flag("unsigned", K("unsigned"))]), attr("unqualified_name", typename), optional(attr("ref", ref_re))


#
def clean_c_symbol_name(name):
	name = name.replace('::', '__')
	return name


#
def _prepare_ctypes(ctypes, template):
	if not type(ctypes) is type([]):
		ctypes = [ctypes]
	return [parse(type, template) for type in ctypes]


#
class _CArg:
	def __repr__(self):
		out = repr(self.ctype)
		if hasattr(self, 'name'):
			out += ' ' + str(self.name)
		return out


_CArg.grammar = attr("ctype", _CType), optional(name())


#
def ctype_ref_to(src_ref, dst_ref):
	i = 0
	while i < len(src_ref) and i < len(dst_ref):
		if src_ref[i] != dst_ref[i]:
			break
		i += 1

	src_ref = src_ref[i:]
	dst_ref = dst_ref[i:]

	if src_ref == '&':
		if dst_ref == '&':
			return ''  # ref to ref
		elif dst_ref == '*':
			return '&'  # ref to ptr
		else:
			return ''  # ref to value
	elif src_ref == '*':
		if dst_ref == '&':
			return '*'  # ptr to ref
		elif dst_ref == '*':
			return ''  # ptr to ptr
		else:
			return '*'  # ptr to value
	else:
		if dst_ref == '&':
			return ''  # value to ref
		elif dst_ref == '*':
			return '&'  # value to ptr
		else:
			return ''  # value to value


def transform_var_ref_to(var, from_ref, to_ref):
	return ctype_ref_to(from_ref, to_ref) + var


class TypeConverter:
	def __init__(self, type, storage_type=None):
		if not storage_type:
			storage_type = type

		self.ctype = parse(type, _CType)
		self.storage_ctype = parse(storage_type, _CType)

		self.clean_name = get_type_clean_name(type)
		self.bound_name = self.clean_name
		self.fully_qualified_name = get_fully_qualified_ctype_name(self.ctype)
		self.type_tag = '__%s_type_tag' % self.clean_name

		self.constructor = None
		self.members = []
		self.methods = []

		self.bases = []  # type derives from the following types

	def get_type_api(self, module_name):
		return ''

	def finalize_type(self):
		return ''

	def to_c_call(self, out_var, in_var_p):
		assert 'to_c_call not implemented in converter'

	def from_c_call(self, ctype, out_var, in_var_p):
		assert 'from_c_call not implemented in converter'

	def prepare_var_for_conv(self, var, var_ref):
		"""Prepare a variable for use with the converter from_c/to_c methods."""
		return transform_var_ref_to(var, var_ref, self.ctype.get_ref('*'))

	def get_all_methods(self):
		"""Return a list of all the type methods (including inherited methods)."""
		all_methods = copy.copy(self.methods)

		def collect_base_methods(base):
			for method in base.methods:
				if not any(m['name'] == method['name'] for m in all_methods):
					all_methods.append(method)

			for _base in base.bases:
				collect_base_methods(_base)

		for base in self.bases:
			collect_base_methods(base)

		return all_methods

	def can_upcast_to(self, type):
		clean_name = get_type_clean_name(type)

		if self.clean_name == clean_name:
			return True

		for base in self.bases:
			if base.can_upcast_to(type):
				return True

		return False


#
class FunctionBindingContext:
	def __init__(self, name):
		self.name = name

	def __repr__(self):
		return 'function %s' % self.name

	def get_proxy_name(self):
		return '_%s__' % clean_c_symbol_name(self.name)


class ConstructorBindingContext:
	def __init__(self, type, conv):
		self.type = type
		self.conv = conv

	def __repr__(self):
		return '%s constructor' % self.type

	def get_proxy_name(self):
		return '_%s__constructor__' % clean_c_symbol_name(self.type)


class MethodBindingContext:
	def __init__(self, type, name, conv):
		self.type = type
		self.name = name
		self.conv = conv

	def __repr__(self):
		return '%s.%s method' % (self.type, self.name)

	def get_proxy_name(self):
		return '_%s__%s__' % (clean_c_symbol_name(self.type), self.name)


#
class FABGen:
	def output_header(self):
		common = "// This file is automatically generated, do not modify manually!\n\n"

		self._source += "// FABgen .cpp\n"
		self._source += common
		self._header += "// FABgen .h\n"
		self._header += common

	def output_includes(self):
		self.add_include('cstdint', True)

		self._source += '{{{__WRAPPER_INCLUDES__}}}\n'

	def start(self, name):
		self._name = name
		self._header, self._source = "", ""

		self.__system_includes, self.__user_includes = [], []

		self.__type_convs = {}
		self.__function_templates = {}

		self._bound_types = []  # list of bound types
		self._bound_functions = []  # list of bound functions

		self.output_header()
		self.output_includes()

		self._source += 'enum OwnershipPolicy { NonOwning, Copy, Owning };\n\n'
		self._source += 'void *_type_tag_upcast(void *in_p, const char *in_type_tag, const char *out_type_tag);\n\n'

	def add_include(self, path, is_system_include = False):
		if is_system_include:
			self.__system_includes.append(path)
		else:
			self.__user_includes.append(path)

	def insert_code(self, code, in_source=True, in_header=True):
		if in_header:
			self._header += code
		if in_source:
			self._source += code

	#
	def raise_exception(self, type, reason):
		assert 'raise_exception not implemented in generator'

	#
	def _begin_type(self, conv):
		"""Declare a new type converter."""
		self._bound_types.append(conv)
		self.__type_convs[conv.fully_qualified_name] = conv
		return conv

	def _end_type(self, conv):
		self._header += conv.get_type_api(self._name)
		self._source += '// %s type glue\n' % conv.fully_qualified_name
		self._source += 'static const char *%s = "%s";\n\n' % (conv.type_tag, conv.fully_qualified_name)
		self._source += conv.get_type_glue(self._name)

	#
	def bind_type(self, conv):
		self._begin_type(conv)
		self._end_type(conv)

	#
	def get_class_default_converter(self):
		assert "missing class type default converter"

	def begin_class(self, name):
		class_default_conv = self.get_class_default_converter()

		conv = class_default_conv(name)
		api = conv.get_type_api(self._name)
		self._source += api + '\n'

		return self._begin_type(conv)

	def end_class(self, name):
		self._end_type(self.__type_convs[name])

	#
	def add_class_base(self, name, base):
		conv = self.__type_convs[name]
		base_conv = self.__type_convs[base]
		conv.bases.append(base_conv)

	#
	def select_ctype_conv(self, ctype):
		"""Select a type converter."""
		full_qualified_ctype_name = get_fully_qualified_ctype_name(ctype)

		if full_qualified_ctype_name == 'void':
			return None

		if full_qualified_ctype_name in self.__type_convs:
			return self.__type_convs[full_qualified_ctype_name]

		return self.__type_convs[ctype.unqualified_name]

	#
	def decl_var(self, ctype, name, end_of_expr=';\n'):
		return '%s %s%s' % (get_fully_qualified_ctype_name(ctype), name, end_of_expr)

	#
	def select_args_convs(self, args):
		return [{'conv': self.select_ctype_conv(arg.ctype), 'ctype': arg.ctype} for i, arg in enumerate(args)]

	#
	def commit_rvals(self, rval):
		assert "missing return values template"

	#
	def __ref_to_ownership_policy(self, ctype):
		return 'Copy' if ctype.get_ref() == '' else 'NonOwning'

	# --
	def prepare_protos(self, protos):
		_protos = []

		for proto in protos:
			rval = parse(proto[0], _CType)
			_proto = {'rval': {'ctype': rval, 'conv': self.select_ctype_conv(rval)}, 'args': []}

			args = proto[1]
			if not type(args) is type([]):
				args = [args]

			for arg in args:
				carg = parse(arg, _CArg)
				conv = self.select_ctype_conv(carg.ctype)
				_proto['args'].append({'carg': carg, 'conv': conv, 'check_var': None})

			_protos.append(_proto)

		return _protos

	def proto_call(self, name, proto, bind_ctx):
		rval = proto['rval']['ctype']
		rval_conv = proto['rval']['conv']

		# prepare C call self argument
		if type(bind_ctx) is MethodBindingContext:
			self._source += '	' + self.decl_var(bind_ctx.conv.storage_ctype, '_self')
			self._source += '	' + bind_ctx.conv.to_c_call(self.get_self(), '&_self')

		# prepare C call arguments
		args = proto['args']
		c_call_args = []

		for i, arg in enumerate(args):
			conv = arg['conv']
			if not conv:
				continue

			arg_name = 'arg%d' % i
			self._source += self.decl_var(conv.storage_ctype, arg_name)
			self._source += conv.to_c_call(self.get_arg(i), '&' + arg_name)

			c_call_arg_transform = ctype_ref_to(conv.storage_ctype.get_ref(), arg['carg'].ctype.get_ref())
			c_call_args.append(c_call_arg_transform + arg_name)

		# declare return value
		if type(bind_ctx) is ConstructorBindingContext:
			fully_qualified_name = get_fully_qualified_ctype_name(rval)

			rval = rval.add_ref('*')  # constructor returns a pointer
			self._source += self.decl_var(rval, 'rval', ' = ')
			self._source += 'new %s(%s);\n' % (fully_qualified_name, ', '.join(c_call_args))

			ownership = 'Owning'  # constructor output is owned by VM
			self.rval_from_c_ptr(rval, 'rval', rval_conv, ctype_ref_to(rval.get_ref(), rval_conv.ctype.get_ref() + '*') + 'rval', ownership)
		else:
			# return value is optional for a function call
			if rval_conv:
				self._source += self.decl_var(rval, 'rval', ' = ')

			if type(bind_ctx) is MethodBindingContext:
				self._source += '_self->%s(%s);\n' % (name, ', '.join(c_call_args))
			else:
				self._source += '%s(%s);\n' % (name, ', '.join(c_call_args))

			if rval_conv:
				ownership = self.__ref_to_ownership_policy(rval)
				self.rval_from_c_ptr(rval, 'rval', rval_conv, ctype_ref_to(rval.get_ref(), rval_conv.ctype.get_ref() + '*') + 'rval', ownership)

		self.commit_rvals(rval)

	def _bind_function_common(self, name, protos, bind_ctx):
		protos = self.prepare_protos(protos)

		# categorize prototypes by number of argument they take
		def get_protos_per_arg_count(protos):
			by_arg_count = {}
			for proto in protos:
				arg_count = len(proto['args'])
				if arg_count not in by_arg_count:
					by_arg_count[arg_count] = []
				by_arg_count[arg_count].append(proto)
			return by_arg_count

		protos_by_arg_count = get_protos_per_arg_count(protos)

		# prepare proxy function
		self.insert_code('// %s\n' % name, True, False)
		proxy_name = bind_ctx.get_proxy_name()

		max_arg_count = max(protos_by_arg_count.keys())

		if type(bind_ctx) is MethodBindingContext:
			self.open_method(proxy_name, max_arg_count)
		else:
			self.open_function(proxy_name, max_arg_count)

		if type(bind_ctx) is ConstructorBindingContext:
			bind_ctx.conv.constructor = {'proxy_name': proxy_name, 'protos': protos}
		elif type(bind_ctx) is MethodBindingContext:
			bind_ctx.conv.methods.append({'name': name, 'proxy_name': proxy_name, 'protos': protos})
		elif type(bind_ctx) is FunctionBindingContext:
			self._bound_functions.append({'name': name, 'proxy_name': proxy_name, 'protos': protos})

		# output dispatching logic
		def get_protos_per_arg_conv(protos, arg_idx):
			per_arg_conv = {}
			for proto in protos:
				arg_conv = proto['args'][arg_idx]['conv']
				if arg_conv not in per_arg_conv:
					per_arg_conv[arg_conv] = []
				per_arg_conv[arg_conv].append(proto)
			return per_arg_conv

		for arg_count, protos_with_arg_count in protos_by_arg_count.items():
			self._source += '	if (arg_count == %d) {\n' % arg_count

			def output_arg_check_and_dispatch(protos, arg_idx, arg_limit):
				indent = '	' * (arg_idx+2)

				if arg_idx == arg_limit:
					assert len(protos) == 1  # there should only be exactly one prototype with a single signature
					self.proto_call(name, protos[0], bind_ctx)
					return

				protos_per_arg_conv = get_protos_per_arg_conv(protos, arg_idx)

				self._source += indent
				for conv, protos_for_conv in protos_per_arg_conv.items():
					self._source += 'if (%s) {\n' % conv.check_call(self.get_arg(arg_idx))
					output_arg_check_and_dispatch(protos_for_conv, arg_idx+1, arg_limit)
					self._source += indent + '} else '

				self._source += '{\n'
				self.set_error('runtime', 'incorrect type for argument %d to %s' % (arg_idx, repr(bind_ctx)))
				self._source += indent + '}\n'

			output_arg_check_and_dispatch(protos_with_arg_count, 0, arg_count)

			self._source += '	} else '

		self._source += '{\n'
		self.set_error('runtime', 'incorrect number of arguments to %s' % repr(bind_ctx))
		self._source += '	}\n'

		#
		self.close_function()
		self._source += '\n'

	#
	def bind_function(self, name, rval, args):
		self.bind_function_overloads(name, [(rval, args)])

	def bind_function_overloads(self, name, protos):
		self._bind_function_common(name, protos, FunctionBindingContext(name))

	#
	def bind_constructor(self, type, args):
		self.bind_constructor_overloads(type, [args])

	def bind_constructor_overloads(self, type, proto_args):
		conv = self.select_ctype_conv(parse(type, _CType))
		protos = [(type, args) for args in proto_args]
		self._bind_function_common('%s__constructor__' % type, protos, ConstructorBindingContext(type, conv))

	#
	def bind_method(self, type, name, rval, args):
		self.bind_method_overloads(type, name, [(rval, args)])

	def bind_method_overloads(self, type, name, protos):
		conv = self.select_ctype_conv(parse(type, _CType))
		self._bind_function_common(name, protos, MethodBindingContext(type, name, conv))

	#
	def bind_member(self, type, member):
		obj = self.select_ctype_conv(parse(type, _CType))

		member = parse(member, _CArg)
		member_conv = self.select_ctype_conv(member.ctype)

		getset_expr = member_conv.prepare_var_for_conv('_self->%s' % member.name, member.ctype.get_ref())  # pointer to the converter supported type

		#
		self._source += '// get/set %s %s::%s\n' % (member.ctype, member_conv.clean_name, member.name)

		# getter
		self.open_getter_function('_%s_get_%s' % (obj.clean_name, member.name))

		self._source += '	' + self.decl_var(obj.storage_ctype, '_self')
		self._source += '	' + obj.to_c_call(self.get_self(), '&_self')

		rval = [member.ctype]
		self.rval_from_c_ptr(member.ctype, 'rval', member_conv, getset_expr, self.__ref_to_ownership_policy(member.ctype))
		self.commit_rvals(rval)
		self.close_getter_function()

		# setter
		arg_vars = self.open_setter_function('_%s_set_%s' % (obj.clean_name, member.name))

		self._source += '	' + self.decl_var(obj.storage_ctype, '_self')
		self._source += '	' + obj.to_c_call(self.get_self(), '&_self')

		self._source += member_conv.to_c_call(self.get_arg(0), getset_expr)
		self.close_setter_function()

		self._source += '\n'

		obj.members.append(member)

	def bind_members(self, type, members):
		for member in members:
			self.bind_member(type, member)

	# global function template
	def decl_function_template(self, tmpl_name, tmpl_args, rval, args):
		self.__function_templates[tmpl_name] = {'tmpl_args': tmpl_args, 'rval': rval, 'args': args}

	def bind_function_template(self, tmpl_name, bound_name, bind_args):
		tmpl = self.__function_templates[tmpl_name]
		tmpl_args = tmpl['tmpl_args']

		assert len(tmpl_args) == len(bind_args)

		def bind_tmpl_arg(arg):
			return bind_args[tmpl_args.index(arg)] if arg in tmpl_args else arg

		bound_rval = bind_tmpl_arg(tmpl['rval'])
		bound_args = [bind_tmpl_arg(arg) for arg in tmpl['args']]

		bound_named_args = ['%s arg%d' % (arg, idx) for idx, arg in enumerate(bound_args)]

		# output wrapper
		self._source += '// %s<%s> wrapper\n' % (tmpl_name, ', '.join(bind_args))
		self._source += 'static %s %s(%s) {\n' % (bound_rval, bound_name, ', '.join(bound_named_args))
		if bound_rval != 'void':
			self._source += 'return '
		self._source += '%s<%s>(%s);\n' % (tmpl_name, ', '.join(bind_args), ', '.join(['arg%d' % i for i in range(len(bound_args))]))
		self._source += '}\n\n'

		# bind wrapper
		self.bind_function(bound_name, bound_rval, bound_args)

	#
	def output_summary(self):
		self._source += '// Bound %d global functions:\n' % len(self._bound_functions)
		for f in self._bound_functions:
			self._source += '//	- %s bound as %s\n' % (f['name'], f['bound_name'])
		self._source += '\n'

	def get_type_tag_cast_function(self):
		downcasts = {}
		for type in self._bound_types:
			downcasts[type] = []

		def register_upcast(type, bases):
			for base in bases:
				downcasts[base].append(type)
				register_upcast(type, base.bases)

		for type in self._bound_types:
			register_upcast(type, type.bases)

		#
		out = '''\
// type_tag based cast system
void *_type_tag_upcast(void *in_p, const char *in_type_tag, const char *out_type_tag) {
	if (out_type_tag == in_type_tag)
		return in_p;

	void *out_p = NULL;
\n'''

		i = 0
		for base in self._bound_types:
			if len(downcasts[base]) == 0:
				continue

			out += '	' if i == 0 else ' else '
			out += 'if (out_type_tag == %s) {\n' % base.type_tag

			for j, downcast in enumerate(downcasts[base]):
				out += '		' if j == 0 else '		else '
				out += 'if (in_type_tag == %s)\n' % downcast.type_tag
				out += '			out_p = (%s *)((%s *)in_p);\n' % (get_fully_qualified_ctype_name(base.ctype), get_fully_qualified_ctype_name(downcast.ctype))

			out += '	}'
			i += 1

		out += '''

	return out_p;
}\n\n'''
		return out

	def finalize(self):
		# insert includes
		system_includes = ''
		if len(self.__system_includes) > 0:
			system_includes = ''.join(['#include <%s>\n' % path for path in self.__system_includes])

		user_includes = ''
		if len(self.__user_includes) > 0:
			user_includes = ''.join(['#include "%s"\n' % path for path in self.__user_includes])

		self._source = self._source.replace('{{{__WRAPPER_INCLUDES__}}}', system_includes + user_includes)

		# cast to
		self._source += self.get_type_tag_cast_function()

	def get_output(self):
		return self._header, self._source
