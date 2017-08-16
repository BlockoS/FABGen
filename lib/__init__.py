def bind_defaults(gen):
	if gen.get_language() == 'CPython':
		import lib.cpython.std
		import lib.cpython.stl

		lib.cpython.std.bind_std(gen)
		lib.cpython.stl.bind_stl(gen)
	elif gen.get_language() == 'Lua':
		import lib.lua.std
		import lib.lua.stl

		lib.lua.std.bind_std(gen)
		lib.lua.stl.bind_stl(gen)